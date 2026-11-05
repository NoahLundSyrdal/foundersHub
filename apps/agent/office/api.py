"""HTTP API. Run: uv run uvicorn office.api:create_app --factory --port 3001 (from apps/agent)."""

import base64
import asyncio
import binascii
import json
import math
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from office import agent, brain, bus, config, db, integrations, voice, procurement_workflow, legacy
from office import facility_api, oda_food, oda_api, work_api
from office.facility_rules import FacilityReading


class Input(BaseModel):
    board_id: str
    text: str | None = None
    audio_b64: str | None = None  # any format OpenAI transcription accepts
    person: str | None = None  # dashboard override / already-identified speaker


class Sensor(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, strict=True)
    board_id: str
    kind: str  # e.g. stock_level, temperature, humidity, distance
    value: float
    event_id: str | None = Field(default=None, min_length=1, max_length=128)
    timestamp: str | None = None
    simulation: bool = False


class Decision(BaseModel):
    approve: bool
    proposal_fingerprint: str | None = None


class DeviceEvent(BaseModel):  # legacy shape still emitted by apps/device/src/bridge.py
    event_id: str
    station_id: str
    type: str  # button_pressed | speech_transcript | stock_low
    timestamp: str
    payload: dict = {}


def create_app(db_path: str = config.DATABASE_PATH) -> FastAPI:
    conn = db.connect(db_path)
    procurement_workflow.ensure_schema(conn)
    agent.recover_interrupted(conn)
    groceries = oda_food.service(conn)
    mutation_lock = asyncio.Lock()
    facilities = facility_api.FacilityService(conn, mutation_lock)
    work = work_api.WorkRuntime(conn, mutation_lock)

    @asynccontextmanager
    async def lifespan(app):
        async with mutation_lock:
            await facilities.tick()
            work.tick()
        monitor = asyncio.create_task(facilities.watch())
        work_monitor = asyncio.create_task(work.watch())
        try:
            yield
        finally:
            oda_food.release(conn)
            monitor.cancel()
            work_monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
            with suppress(asyncio.CancelledError):
                await work_monitor

    app = FastAPI(title="Office agent", lifespan=lifespan)
    app.state.conn = conn
    app.state.mutation_lock = mutation_lock
    app.state.facilities = facilities
    app.state.oda_food = groceries
    app.state.work = work
    app.include_router(legacy.router(conn))
    app.include_router(facility_api.router(facilities))
    app.include_router(oda_api.router(groceries))
    app.include_router(work_api.router(work))

    def board_or_404(board_id: str) -> config.Board:
        if board_id not in config.BOARDS:
            raise HTTPException(404, f"Unknown board {board_id!r}")
        return config.BOARDS[board_id]

    async def handle_text(board_id: str, text: str, person: str | None) -> dict:
        task_id = agent.start_task(conn, board_id, person or config.GUEST.name, text)
        return await agent.run(conn, task_id, text)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "model": config.MODEL, "orders": "simulated", "agent_provider": config.MODEL, "supplier_provider": "mock", "simulation": True,
                "brain": "notion snapshot" if brain.source() == brain.SNAPSHOT else "local copy (config/brain)", "integrations": integrations.status()}

    @app.get("/api/brain")
    async def get_brain():
        """Company brain in use: where it came from, the approval routing table, and the procedure documents."""
        return {"source": str(brain.source().relative_to(config.ROOT)), "routing": brain.routing(), "docs": brain.docs()}

    @app.post("/api/brain/sync")
    async def sync_brain():
        """Re-read the brain from Notion (NOTION_BRAIN_PAGE_ID). 409 if not connected or the routing table is invalid."""
        try:
            return await brain.sync()
        except brain.BrainError as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/api/boards")
    async def boards():
        return {
            "boards": [b.__dict__ | {"modules": sorted(b.modules)} for b in config.BOARDS.values()],
            "people": [p.__dict__ for p in config.PEOPLE.values()],
        }

    @app.get("/api/state")
    async def state():
        return {
            "tasks": [procurement_workflow.enrich_task(conn, db.task_view(t)) for t in db.rows(conn, "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100")],
            "orders": db.rows(conn, "SELECT * FROM orders ORDER BY created_at DESC"),
            "tickets": db.rows(conn, "SELECT * FROM tickets ORDER BY created_at DESC"),
            "commands": {c["board_id"]: c for c in db.rows(conn, "SELECT * FROM board_commands")},
        }

    @app.post("/api/input")
    async def input_(body: Input):
        board_or_404(body.board_id)
        text, person = body.text, body.person
        if body.audio_b64:
            try:
                audio = base64.b64decode(body.audio_b64, validate=True)
            except binascii.Error:
                raise HTTPException(400, "audio_b64 is not valid base64")
            text = text or await asyncio.to_thread(voice.transcribe, audio)  # network call: keep the loop free
            person = person or await asyncio.to_thread(voice.identify, audio)
        if not text or not text.strip():
            raise HTTPException(400, "Need text or audio")
        async with mutation_lock:
            return await handle_text(body.board_id, text.strip(), person)

    @app.post("/api/sensor")
    async def sensor(body: Sensor):
        async with mutation_lock:
            board = board_or_404(body.board_id)
            metric = facility_api.metric_for(body.kind)
            if metric:
                try:
                    reading = FacilityReading.model_validate({
                        "event_id": body.event_id or db.new_id(), "board_id": board.id,
                        "metric": metric, "value": body.value,
                        "timestamp": body.timestamp or facilities.engine.snapshot()["evaluated_at"],
                        "simulation": body.simulation,
                    })
                    result = await facilities.ingest(reading)
                    # Preserve the native stock sensor response while replacing its
                    # old prompt-based trigger with the same fixed rule dispatcher.
                    if any(t["kind"] == body.kind for t in board.triggers):
                        for incident in result["incidents"]:
                            action = incident["action_results"].get("replenish", {})
                            if incident["status"] == "active" and action.get("task_id"):
                                task = db.task_view(db.row(conn, "SELECT * FROM tasks WHERE id=?", action["task_id"]))
                                return procurement_workflow.enrich_task(conn, task) | {"command": legacy.response(conn, task["id"])["command"]}
                        return {"stored": True}
                    return {"stored": True, **result}
                except ValueError as error:
                    raise HTTPException(400, str(error)) from error
            conn.execute("INSERT INTO sensor_readings VALUES (?, ?, ?, ?)", (board.id, body.kind, body.value, db.now()))
            return {"stored": True}

    @app.post("/api/tasks/{task_id}/decide")
    async def decide(task_id: str, body: Decision):
        try:
            async with mutation_lock:
                return await agent.decide(conn, task_id, body.approve, body.proposal_fingerprint)
        except agent.StaleApproval as e:
            raise HTTPException(409, str(e))

    @app.get("/api/alerts")
    async def alerts():
        return procurement_workflow.list_alerts(conn)

    @app.post("/api/alerts/{alert_id}/acknowledge")
    async def acknowledge(alert_id: str):
        async with mutation_lock:
            try:
                return procurement_workflow.acknowledge_alert(conn, alert_id)
            except procurement_workflow.ProcurementConflict as error:
                raise HTTPException(404, str(error))

    @app.post("/api/orders/{order_id}/delivered")
    async def delivered(order_id: str):
        conn.execute("UPDATE orders SET status = 'delivered' WHERE id = ?", (order_id,))
        bus.publish("state.changed", {})
        return {"ok": True}

    @app.post("/api/tickets/{ticket_id}/resolve")
    async def resolve(ticket_id: str):
        conn.execute("UPDATE tickets SET status = 'resolved' WHERE id = ?", (ticket_id,))
        bus.publish("state.changed", {})
        return {"ok": True}

    @app.get("/api/events")
    async def events():
        return StreamingResponse(bus.subscribe(), media_type="text/event-stream")

    # --- Device bridge compatibility (apps/device/src/bridge.py) ---

    @app.post("/api/device-events")
    async def device_event(ev: DeviceEvent):
        board = board_or_404(ev.station_id)
        async with mutation_lock:
            seen = db.row(conn, "SELECT task_id FROM device_events WHERE event_id=?", ev.event_id)
            if seen:
                try:
                    facilities.validate_device_retry(ev.event_id, ev.model_dump())
                except ValueError as error:
                    raise HTTPException(409, str(error)) from error
                if ev.type == "stock_low":
                    await facilities.resume_stock_checks()
                return legacy.response(conn, seen["task_id"], True)
            if ev.type not in {"button_pressed", "speech_transcript", "stock_low"}:
                raise HTTPException(400, "Unsupported event type")
            if ev.type == "speech_transcript":
                text = str(ev.payload.get("text", "")).strip()
                if not text or len(text) > 2000:
                    raise HTTPException(400, "Speech events require bounded text")
            elif ev.type == "stock_low":
                item_id = ev.payload.get("item_id")
                try:
                    facilities.validate_stock_item(board.id, item_id)
                    level = ev.payload.get("level")
                    if level is not None and (type(level) not in (int, float) or not math.isfinite(level) or not 0 <= level <= 100):
                        raise ValueError("Stock level must be a finite percentage between 0 and 100")
                    if type(ev.payload.get("simulation", True)) is not bool:
                        raise ValueError("simulation must be a boolean")
                except ValueError as error:
                    raise HTTPException(400, str(error)) from error
                # Pack sizes/readings are not requested quantities.
                text = f"[sensor] Need 1 {item_id.replace('-', ' ')} at {board.location}."
            else:
                text = ""
            task_id = db.new_id()
            status = "awaiting_input" if ev.type == "button_pressed" else "running"
            conn.execute("BEGIN")
            try:
                conn.execute("INSERT INTO tasks(id,board_id,person,prompt,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                             (task_id,board.id,config.GUEST.name,text,status,db.now(),db.now()))
                conn.execute("INSERT INTO device_events VALUES(?,?,?)", (ev.event_id,task_id,"{}"))
                facilities.remember_device_input(ev.event_id, ev.model_dump())
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            if ev.type == "button_pressed":
                db.set_command(conn,board.id,"Describe what you need","blue")
                bus.publish("board.listen", {"board_id":board.id})
                bus.publish("requests.updated", {"request_id":task_id}, source=conn)
            elif ev.type == "stock_low":
                await facilities.prepare_stock_check(task_id, board.id, ev.payload["item_id"], ev.payload.get("simulation", True))
            else:
                await agent.run(conn,task_id,text)
            result = legacy.response(conn,task_id)
            conn.execute("UPDATE device_events SET response=? WHERE event_id=?", (json.dumps(result),ev.event_id))
            return result

    @app.get("/api/devices/{board_id}/command")
    async def device_command(board_id: str):
        c = db.row(conn, "SELECT * FROM board_commands WHERE board_id = ?", board_id)
        return {"command": {"station_id": board_id, "text": c["text"], "color": c["color"]} if c else None}

    return app
