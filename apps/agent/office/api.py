"""HTTP API. Run: uv run uvicorn office.api:create_app --factory --port 3001 (from apps/agent)."""

import base64
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from office import agent, bus, config, db, voice


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


class DeviceEvent(BaseModel):  # legacy shape still emitted by apps/device/src/bridge.py
    event_id: str
    station_id: str
    type: str  # button_pressed | speech_transcript | stock_low
    timestamp: str
    payload: dict = {}


def create_app(db_path: str = config.DATABASE_PATH) -> FastAPI:
    conn = db.connect(db_path)
    app = FastAPI(title="Office agent")

    def board_or_404(board_id: str) -> config.Board:
        if board_id not in config.BOARDS:
            raise HTTPException(404, f"Unknown board {board_id!r}")
        return config.BOARDS[board_id]

    async def handle_text(board_id: str, text: str, person: str | None) -> dict:
        task_id = agent.start_task(conn, board_id, person or config.GUEST.name, text)
        return await agent.run(conn, task_id, text)

    @app.get("/api/health")
    def health():
        return {"ok": True, "model": config.MODEL, "orders": "simulated"}

    @app.get("/api/boards")
    def boards():
        return {
            "boards": [b.__dict__ | {"modules": sorted(b.modules)} for b in config.BOARDS.values()],
            "people": [p.__dict__ for p in config.PEOPLE.values()],
        }

    @app.get("/api/state")
    def state():
        return {
            "tasks": [db.task_view(t) for t in db.rows(conn, "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100")],
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
        return await handle_text(body.board_id, text.strip(), person)

    @app.post("/api/sensor")
    async def sensor(body: Sensor):
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
            return await agent.decide(conn, task_id, body.approve)
        except agent.StaleApproval as e:
            raise HTTPException(409, str(e))

    @app.post("/api/orders/{order_id}/delivered")
    def delivered(order_id: str):
        conn.execute("UPDATE orders SET status = 'delivered' WHERE id = ?", (order_id,))
        bus.publish("state.changed", {})
        return {"ok": True}

    @app.post("/api/tickets/{ticket_id}/resolve")
    def resolve(ticket_id: str):
        conn.execute("UPDATE tickets SET status = 'resolved' WHERE id = ?", (ticket_id,))
        bus.publish("state.changed", {})
        return {"ok": True}

    @app.get("/api/events")
    def events():
        return StreamingResponse(bus.subscribe(), media_type="text/event-stream")

    # --- Device bridge compatibility (apps/device/src/bridge.py) ---

    @app.post("/api/device-events")
    async def device_event(ev: DeviceEvent):
        board = board_or_404(ev.station_id)
        seen = db.row(conn, "SELECT response FROM device_events WHERE event_id = ?", ev.event_id)
        if seen:  # transport retry: same event id never creates a second task
            return json.loads(seen["response"]) | {"duplicate": True}
        if ev.type == "button_pressed":  # push-to-talk: a voice client listening on SSE starts recording
            bus.publish("board.listen", {"board_id": board.id})
            result = {"request": {"id": ev.event_id}, "command": db.set_command(conn, board.id, "Listening...", "blue")}
        elif ev.type == "speech_transcript":
            task = await handle_text(board.id, ev.payload.get("text", ""), ev.payload.get("person"))
            result = {"request": {"id": task["id"]}, "command": task["command"]}
        elif ev.type == "stock_low":
            item = config.PRODUCTS.get(ev.payload.get("item_id"), {}).get("name", ev.payload.get("item_id"))
            prompt = f"[sensor] {item} is running low at the {board.location} ({ev.payload.get('level', '?')}%)."
            task = await handle_text(board.id, prompt, "sensor")
            result = {"request": {"id": task["id"]}, "command": task["command"]}
        else:
            raise HTTPException(400, f"Unsupported event type {ev.type!r}")
        result["duplicate"] = False
        conn.execute("INSERT INTO device_events VALUES (?, ?, ?)", (ev.event_id, result["request"]["id"], json.dumps(result)))
        return result

    @app.get("/api/devices/{board_id}/command")
    def device_command(board_id: str):
        c = db.row(conn, "SELECT * FROM board_commands WHERE board_id = ?", board_id)
        return {"command": {"station_id": board_id, "text": c["text"], "color": c["color"]} if c else None}

    return app
