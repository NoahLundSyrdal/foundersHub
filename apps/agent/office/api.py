"""HTTP API. Run: uv run uvicorn office.api:create_app --factory --port 3001 (from apps/agent)."""

import base64
import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from office import agent, bus, config, db, voice, procurement_workflow, legacy, oda_food, oda_api


class Input(BaseModel):
    board_id: str
    text: str | None = None
    audio_b64: str | None = None  # any format OpenAI transcription accepts
    person: str | None = None  # dashboard override / already-identified speaker


class Sensor(BaseModel):
    board_id: str
    kind: str  # e.g. stock_level, temperature, humidity, distance
    value: float


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
    groceries = oda_food.service(conn)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            oda_food.release(conn)

    app = FastAPI(title="Office agent", lifespan=lifespan)
    procurement_workflow.ensure_schema(conn)
    mutation_lock = asyncio.Lock()
    app.state.conn = conn
    app.state.mutation_lock = mutation_lock
    app.state.oda_food = groceries
    app.include_router(legacy.router(conn))
    app.include_router(oda_api.router(groceries))

    def board_or_404(board_id: str) -> config.Board:
        if board_id not in config.BOARDS:
            raise HTTPException(404, f"Unknown board {board_id!r}")
        return config.BOARDS[board_id]

    async def handle_text(board_id: str, text: str, person: str | None) -> dict:
        task_id = agent.start_task(conn, board_id, person or config.GUEST.name, text)
        return await agent.run(conn, task_id, text)

    @app.get("/api/health")
    async def health():
        return {"ok": True, "model": config.MODEL, "orders": "simulated", "agent_provider": config.MODEL, "supplier_provider": "mock", "simulation": True}

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
            audio = base64.b64decode(body.audio_b64)
            text = text or voice.transcribe(audio)
            person = person or voice.identify(audio)
        if not text or not text.strip():
            raise HTTPException(400, "Need text or audio")
        async with mutation_lock:
            return await handle_text(body.board_id, text.strip(), person)

    @app.post("/api/sensor")
    async def sensor(body: Sensor):
        async with mutation_lock:
            board = board_or_404(body.board_id)
            prev = db.row(conn, "SELECT value FROM sensor_readings WHERE board_id = ? AND kind = ? ORDER BY at DESC, rowid DESC LIMIT 1", board.id, body.kind)
            conn.execute("INSERT INTO sensor_readings VALUES (?, ?, ?, ?)", (board.id, body.kind, body.value, db.now()))
            for trig in board.triggers:
                crossed = body.value < trig["below"] and (prev is None or prev["value"] >= trig["below"])
                if trig["kind"] == body.kind and crossed:  # fire on the transition only, not on every low reading
                    return await handle_text(board.id, trig["prompt"].format(value=round(body.value)), "sensor")
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
                return legacy.response(conn, seen["task_id"], True)
            if ev.type not in {"button_pressed", "speech_transcript", "stock_low"}:
                raise HTTPException(400, "Unsupported event type")
            if ev.type == "speech_transcript":
                text = str(ev.payload.get("text", "")).strip()
                if not text or len(text) > 2000:
                    raise HTTPException(400, "Speech events require bounded text")
            elif ev.type == "stock_low":
                item_id = ev.payload.get("item_id")
                if item_id not in config.PRODUCTS:
                    raise HTTPException(400, "Stock events require a configured item")
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
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            if ev.type == "button_pressed":
                db.set_command(conn,board.id,"Describe what you need","blue")
                bus.publish("board.listen", {"board_id":board.id})
                bus.publish("requests.updated", {"request_id":task_id})
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
