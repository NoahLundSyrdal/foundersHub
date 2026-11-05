"""The office agent and the task lifecycle: run → (maybe pause for approval) → resume."""

import json
from datetime import UTC, datetime, timedelta

from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolDenied,
)
from pydantic_ai.capabilities import Hooks
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.function import FunctionModel

from office import bus, config, db, mock, plugins
from office.deps import Deps

INSTRUCTIONS = """\
You are the office assistant, living in small devices placed around the office.
People hand you tasks by voice. Your job is to make things happen with zero friction:
- If the request is clear enough, act immediately with your tools. Don't ask for confirmation;
  the system asks a human for approval when policy requires it.
- Ask a question only if you truly cannot act.
- Your final reply is spoken aloud and shown on a tiny screen: ONE short sentence, max ~20 words.
- Orders are simulated in this demo. When you order something, say it was ordered (simulated).
- Never invent products or prices; use the catalogue tools.
"""

# Live activity feed for the dashboard: what the agent is doing, not its private reasoning.
progress = Hooks()


@progress.on.before_tool_execute
async def tool_called(ctx: RunContext[Deps], *, call, tool_def, args):
    bus.publish("tool.called", {"task_id": ctx.deps.task_id, "tool": call.tool_name, "args": call.args_as_dict()})
    return args


@progress.on.after_tool_execute
async def tool_result(ctx: RunContext[Deps], *, call, tool_def, args, result):
    bus.publish("tool.result", {"task_id": ctx.deps.task_id, "tool": call.tool_name, "content": str(result)[:300]})
    return result


model = FunctionModel(mock.respond) if config.MODEL == "mock" else config.MODEL
office_agent = Agent(
    model, deps_type=Deps, output_type=[str, DeferredToolRequests], instructions=INSTRUCTIONS, capabilities=[progress]
)


@office_agent.instructions
def context(ctx: RunContext[Deps]) -> str:
    b, p = ctx.deps.board, ctx.deps.person
    others = ", ".join(f"{x.id} ({x.location}: {', '.join(sorted(x.modules))})" for x in config.BOARDS.values())
    return (
        f"Now: {datetime.now().strftime('%A %Y-%m-%d %H:%M')}.\n"
        f"Speaker: {p.name}, role {p.role}, may auto-approve orders up to {p.auto_approve_nok} NOK.\n"
        f"You are at board '{b.id}' in the {b.location}. Equipment here: {', '.join(b.equipment)}. "
        f"Responsible team: {b.responsible_team}.\n"
        f"All boards: {others}.\n"
        f"Office policy: max {config.OFFICE['max_order_nok']} NOK per order; {'; '.join(config.OFFICE['preferences'])}."
    )


class StaleApproval(Exception):
    pass


def _update(conn, task_id: str, **fields) -> dict:
    fields["updated_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
    task = db.task_view(db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id))
    bus.publish("task.updated", task)
    return task


def start_task(conn, board_id: str, person_name: str, text: str) -> str:
    """New task, or continue the board's last task when it just asked *this same person* a question.

    Never continue across speakers (that would borrow their approval limit) or for sensor-triggered tasks.
    """
    recent = db.row(
        conn,
        "SELECT * FROM tasks WHERE board_id = ? AND person = ? AND person != 'sensor' AND status = 'done' "
        "AND reply LIKE '%?' AND updated_at > ? ORDER BY updated_at DESC LIMIT 1",
        board_id,
        person_name,
        (datetime.now(UTC) - timedelta(minutes=2)).isoformat(timespec="seconds"),
    )
    if recent:
        return recent["id"]
    task_id = db.new_id()
    conn.execute(
        "INSERT INTO tasks (id, board_id, person, prompt, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'running', ?, ?)",
        (task_id, board_id, person_name, text, db.now(), db.now()),
    )
    return task_id


def recover(conn) -> None:
    """At startup: a task left 'running' was interrupted mid-run. Fail it visibly; its orders (if any) are in `orders`."""
    for t in db.rows(conn, "SELECT id FROM tasks WHERE status = 'running'"):
        placed = db.rows(conn, "SELECT ref FROM orders WHERE task_id = ?", t["id"])
        note = f" Orders placed before the interruption: {', '.join(o['ref'] for o in placed)}." if placed else " No order was placed."
        _update(conn, t["id"], status="failed", pending="[]", reply="Interrupted by a server restart." + note)


async def run(conn, task_id: str, prompt: str | None, deferred: DeferredToolResults | None = None) -> dict:
    t = db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id)
    board = config.BOARDS[t["board_id"]]
    deps = Deps(board=board, person=config.person(t["person"]), task_id=task_id, db=conn)
    history = ModelMessagesTypeAdapter.validate_json(t["messages"])
    _update(conn, task_id, status="running")

    try:
        result = await office_agent.run(
            prompt,
            deps=deps,
            toolsets=plugins.for_board(board),
            message_history=history or None,
            deferred_tool_results=deferred,
        )
    except Exception as e:  # demo: surface any failure on the board instead of crashing the request
        placed = db.rows(conn, "SELECT ref FROM orders WHERE task_id = ?", task_id)
        reply = f"Sorry, something went wrong ({type(e).__name__})."
        if placed:  # the failure may come after an order was placed: never hide that
            reply += f" Already ordered (simulated): {', '.join(o['ref'] for o in placed)}."
        bus.publish("board.say", {"board_id": board.id, "text": reply})
        return _update(conn, task_id, status="failed", pending="[]", reply=f"{reply} {e}") | {
            "command": db.set_command(conn, board.id, reply, "red")
        }

    out = result.output
    if isinstance(out, DeferredToolRequests):
        pending = [
            {"tool_call_id": c.tool_call_id, "tool": c.tool_name, "args": c.args_as_dict(), "details": out.metadata.get(c.tool_call_id, {})}
            for c in out.approvals
        ]
        reply = "Waiting for approval: " + "; ".join(p["details"].get("summary", p["tool"]) for p in pending)
        status, color = "awaiting_approval", "amber"
    else:
        pending, reply, status, color = [], out, "done", "green"

    command = db.set_command(conn, board.id, reply, color)
    bus.publish("board.say", {"board_id": board.id, "text": reply})
    task = _update(conn, task_id, status=status, reply=reply, pending=json.dumps(pending), messages=result.all_messages_json().decode())
    return task | {"command": command}


async def decide(conn, task_id: str, approve: bool) -> dict:
    """Resolve the stored pending calls. Args come from the DB, never the client, so approval = exactly what was shown."""
    t = db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id)
    if not t or t["status"] != "awaiting_approval":
        raise StaleApproval("Task is not awaiting approval")
    pending = json.loads(t["pending"])
    for p in pending:  # the approver saw a price; refuse if the catalogue changed since
        product = config.PRODUCTS.get(p["args"].get("product_id"), {})
        shown = p["details"].get("total_nok")
        if shown is not None and product.get("price_nok", 0) * p["args"].get("quantity", 1) != shown:
            _update(conn, task_id, status="failed", pending="[]", reply="Price changed since approval was requested. Ask again.")
            raise StaleApproval("Price changed since it was shown")
    conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))  # claim it before any await
    results = DeferredToolResults(
        approvals={p["tool_call_id"]: True if approve else ToolDenied("A human denied this.") for p in pending}
    )
    return await run(conn, task_id, None, deferred=results)
