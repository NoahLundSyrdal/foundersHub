"""Dispatch validated needs directly; approvals never need another model turn."""
import json
from types import SimpleNamespace

from office import bus, config, db, procurement_workflow as workflow
from office.deps import Deps


def finish(conn, task_id, status, reply, pending=None, color=None):
    conn.execute("UPDATE tasks SET status=?,reply=?,pending=?,updated_at=? WHERE id=?",
                 (status, reply, json.dumps(pending or []), db.now(), task_id))
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    color = color or ("amber" if status == "awaiting_approval" else "red" if status == "failed" else "green")
    command = db.set_command(conn, task["board_id"], reply, color)
    result = workflow.enrich_task(conn, db.task_view(task))
    bus.publish("task.updated", result, source=conn)
    bus.publish("requests.updated", {"request_id": task_id}, source=conn)
    bus.publish("board.say", {"board_id": task["board_id"], "text": reply})
    return result | {"command": command}


async def execute_quote(conn, task_id, call_id, approved=False):
    # Reuse the sole supplier boundary, preserving every current quote/policy check.
    from office.plugins.procurement import place_order
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    value = workflow.get_context(conn, task_id)
    proposal = value["proposal"]
    ctx = SimpleNamespace(
        deps=Deps(config.BOARDS[task["board_id"]], config.person(task["person"]), task_id, conn),
        tool_call_id=call_id, tool_call_approved=approved,
    )
    reply = await place_order(ctx, proposal["product"]["id"], proposal["quantity"])
    order = db.row(conn, "SELECT id FROM orders WHERE task_id=?", task_id)
    return finish(conn, task_id, "done" if order else "failed", reply)


async def propose(conn, task_id):
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    value, _ = workflow.validate_quote(conn, task_id)
    proposal = value["proposal"]
    call_id = f"prepared-order-{task_id}"
    if value["approval_required"]:
        details = workflow.approval_metadata(value, task["person"])
        pending = [{"tool_call_id": call_id, "tool": "place_order",
                    "args": {"product_id": proposal["product"]["id"], "quantity": proposal["quantity"]},
                    "details": details}]
        return finish(conn, task_id, "awaiting_approval", "Waiting for approval: " + details["summary"], pending)
    return await execute_quote(conn, task_id, call_id)


async def decide(conn, task_id, approve, fingerprint=None):
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    if not task or task["status"] != "awaiting_approval":
        raise workflow.ProcurementConflict("Task is not awaiting approval")
    if not approve:
        return finish(conn, task_id, "done", "Request declined; no order was placed.")
    pending = json.loads(task["pending"])
    workflow.authorize_pending(conn, task_id, pending, fingerprint)
    call = next((call for call in pending if call["tool"] == "place_order"), None)
    if not call:
        raise workflow.ProcurementConflict("Stored purchase approval is missing")
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
    return await execute_quote(conn, task_id, call["tool_call_id"], approved=True)


def ticket(conn, task_id, summary):
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    team = config.BOARDS[task["board_id"]].responsible_team
    if not db.row(conn, "SELECT id FROM tickets WHERE task_id=?", task_id):
        conn.execute("INSERT INTO tickets VALUES(?,?,?,?,?,'open',?)",
                     (db.new_id(), task_id, task["board_id"], summary, team, db.now()))
    return finish(conn, task_id, "done", f"Ticket created for {team} (local record; nobody has been contacted).")
