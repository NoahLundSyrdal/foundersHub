"""Informational sensor answers on the normal task API, outside purchasing."""
import json

from office import bus, db
from office.room_conditions import RoomConditions, condition_question, format_readings


def _schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS environment_queries(task_id TEXT PRIMARY KEY,body TEXT NOT NULL)")


def overlay(conn, task_id):
    if not db.row(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='environment_queries'"):
        return {}
    row = db.row(conn, "SELECT body FROM environment_queries WHERE task_id=?", task_id)
    if not row:
        return {}
    value = json.loads(row["body"])
    if value.get("conversational"):
        return {"environment": value["environment"], "trace": value["trace"]}
    return {"status": "resolved", "intent": None, "question": None, "simulation": value["environment"]["current"].get("simulation", False),
            "summary": value["reply"], "environment": value["environment"], "trace": value["trace"]}


def record(conn, task_id, environment):
    """Persist grounded facts for Claude; do not substitute a scripted answer."""
    _schema(conn)
    trace = [{"stage": "conditions_checked", "detail": "Read room conditions and freshness.", "at": db.now()}]
    value = {"conversational": True, "environment": environment, "trace": trace}
    conn.execute("INSERT INTO environment_queries VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET body=excluded.body",
                 (task_id, json.dumps(value, allow_nan=False)))
    bus.publish("environment.progress", {"task_id": task_id, **trace[0]}, source=conn)


def answer(conn, task_id, text):
    if not condition_question(text):
        return None
    _schema(conn)
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    trace = [{"stage": "checking_conditions", "detail": "Let me check the room sensors.", "at": db.now()}]
    bus.publish("board.say", {"board_id": task["board_id"], "text": trace[0]["detail"]}, source=conn)
    bus.publish("environment.progress", {"task_id": task_id, **trace[0]}, source=conn)
    readings = RoomConditions(conn).snapshot(task["board_id"])
    reply = format_readings(readings, text)
    trace.append({"stage": "conditions_checked", "detail": reply, "at": db.now()})
    value = {"reply": reply, "environment": {"current": readings}, "trace": trace}
    conn.execute("INSERT INTO environment_queries VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET body=excluded.body",
                 (task_id, json.dumps(value, allow_nan=False)))
    bus.publish("board.say", {"board_id": task["board_id"], "text": reply}, source=conn)
    return value
