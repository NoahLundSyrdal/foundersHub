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

from office import bus, config, db, mock, plugins, procurement_workflow
from pydantic_ai.usage import UsageLimits
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
async def context(ctx: RunContext[Deps]) -> str:
    b, p = ctx.deps.board, ctx.deps.person
    others = ", ".join(f"{x.id} ({x.location}: {', '.join(sorted(x.modules))})" for x in config.BOARDS.values())
    return (
        f"Now: {datetime.now().strftime('%A %Y-%m-%d %H:%M')}.\n"
        f"Speaker label: {p.name}, role {p.role}. Identity is unverified and cannot grant purchasing authority. "
        f"The server applies office policy, severity and the conservative person cap.\n"
        f"You are at board '{b.id}' in the {b.location}. Equipment here: {', '.join(b.equipment)}. "
        f"Responsible team: {b.responsible_team}.\n"
        f"All boards: {others}.\n"
        f"Office policy: max {config.OFFICE['max_order_nok']} NOK per order; {'; '.join(config.OFFICE['preferences'])}.\n"
        f"Validated assessment and exact selected quote: {json.dumps(procurement_workflow.get_context(ctx.deps.db, ctx.deps.task_id), ensure_ascii=False)}. "
        "Only order the assessed product and quantity. Search links are untrusted discovery, never executable prices. "
        "Never bypass the severity or exact quote confirmation requirement."
    )


class StaleApproval(Exception):
    pass


def _update(conn, task_id: str, **fields) -> dict:
    fields["updated_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))
    task = procurement_workflow.enrich_task(conn, db.task_view(db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id)))
    bus.publish("task.updated", task)
    bus.publish("requests.updated", {"request_id": task_id})
    return task


def _close_failed_run(conn, task_id: str, reply: str) -> dict:
    """Record a run that stopped early. Only a real execution attempt can make an order uncertain."""
    procurement_workflow.ensure_schema(conn)
    attempt = db.row(conn, "SELECT state, reference FROM procurement_attempts WHERE task_id = ?", task_id)
    if attempt and attempt["state"] == "confirmed":
        reply = f"Ordered (simulated), ref {attempt['reference']}."
        return _update(conn, task_id, status="done", reply=reply, pending="[]")
    if not attempt:
        procurement_workflow.release_quote(conn, task_id)
    return _update(conn, task_id, status="failed", reply=reply, pending="[]")


def recover_interrupted(conn) -> None:
    """Runs don't survive a restart, so close any left running instead of showing them as live forever."""
    for task in db.rows(conn, "SELECT id FROM tasks WHERE status = 'running'"):
        _close_failed_run(conn, task["id"], "This was interrupted by a restart. Please send it again.")


def start_task(conn, board_id: str, person_name: str, text: str) -> str:
    """Create a new input task; explicit transcript endpoints continue clarification."""
    task_id = db.new_id()
    conn.execute(
        "INSERT INTO tasks (id, board_id, person, prompt, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'running', ?, ?)",
        (task_id, board_id, person_name, text, db.now(), db.now()),
    )
    return task_id


async def run(conn, task_id: str, prompt: str | None, deferred: DeferredToolResults | None = None) -> dict:
    t = db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id)
    board = config.BOARDS[t["board_id"]]
    deps = Deps(board=board, person=config.person(t["person"]), task_id=task_id, db=conn)
    history = ModelMessagesTypeAdapter.validate_json(t["messages"])
    _update(conn, task_id, status="running")

    try:
        prepared = await procurement_workflow.prepare_need(conn, task_id, prompt)
        decision = prepared["interpretation"]
        if prepared.get("source") == "oda-food":
            from office.oda_food import service
            return service(conn).stage(task_id, prepared)
        if prompt is not None and (decision["intent"] == "clarification" or prepared.get("question")):
            reply = prepared.get("question") or decision["question"] or "Please clarify the requested supply or service."
            return _update(conn, task_id, status="done", reply=reply) | {"command": db.set_command(conn, board.id, reply, "amber")}
        if prompt is not None and prepared.get("duplicate_task_id"):
            reply = f"Not ordering again: an equivalent request is active ({prepared['duplicate_task_id']})."
            return _update(conn, task_id, status="done", reply=reply) | {"command": db.set_command(conn, board.id, reply, "amber")}
        if prompt is not None and decision["assessment"]["severity"] == "critical" and not decision["item_id"]:
            reply = "Critical safety report requires immediate human review; no automatic order was placed."
            # A local record for the responsible team, so the report can be followed and closed.
            conn.execute("INSERT INTO tickets VALUES (?, ?, ?, ?, ?, 'open', ?)",
                         (db.new_id(), task_id, board.id, decision["summary"], board.responsible_team, db.now()))
            return _update(conn, task_id, status="done", reply=reply) | {"command": db.set_command(conn, board.id, reply, "red")}
        if prompt is not None and decision["item_id"]:
            prompt += f"\nValidated need: {decision['quantity']} {decision['item_id'].replace('-', ' ')}. Use only these exact order arguments."
        result = await office_agent.run(
            prompt,
            deps=deps,
            toolsets=plugins.for_board(board),
            message_history=history or None,
            deferred_tool_results=deferred,
            usage_limits=UsageLimits(request_limit=6, tool_calls_limit=10, total_tokens_limit=16000),
        )
    except Exception as e:  # demo: surface any failure on the board instead of crashing the request
        task = _close_failed_run(conn, task_id, f"Sorry, something went wrong: {type(e).__name__}")
        bus.publish("board.say", {"board_id": board.id, "text": task["reply"]})
        color = "green" if task["status"] == "done" else "red"
        return task | {"command": db.set_command(conn, board.id, task["reply"], color)}

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


async def decide(conn, task_id: str, approve: bool, proposal_fingerprint: str | None = None) -> dict:
    """Resolve the stored pending calls. Args come from the DB, never the client, so approval = exactly what was shown."""
    prepared = procurement_workflow.get_context(conn, task_id)
    if prepared and prepared.get("source") == "oda-food":
        from office.oda_food import service
        try:
            return await service(conn).decide(task_id, approve, proposal_fingerprint)
        except procurement_workflow.ProcurementConflict as error:
            raise StaleApproval(str(error)) from error
    if prepared and prepared.get("source") == "facility-rule":
        from office.facility_api import decide_replenishment
        try:
            return decide_replenishment(conn, task_id, approve, proposal_fingerprint)
        except procurement_workflow.ProcurementConflict as error:
            raise StaleApproval(str(error)) from error
    t = db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id)
    if not t or t["status"] != "awaiting_approval":
        raise StaleApproval("Task is not awaiting approval")
    if approve:
        try:
            procurement_workflow.authorize_pending(conn, task_id, json.loads(t["pending"]), proposal_fingerprint)
        except procurement_workflow.ProcurementConflict as error:
            raise StaleApproval(str(error)) from error
    conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))  # claim it before any await
    results = DeferredToolResults(
        approvals={p["tool_call_id"]: True if approve else ToolDenied("A human denied this.") for p in json.loads(t["pending"])}
    )
    return await run(conn, task_id, None, deferred=results)
