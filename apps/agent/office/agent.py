"""The office agent and the task lifecycle: run → (maybe pause for approval) → resume.

Beyond one message: the harness `Memory` keeps an office notebook across tasks and restarts, and the company brain
(office/brain.py, from Notion) gives it the procedures and who approves what.
"""

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
from pydantic_ai_harness import Memory, ToolOutputLimits
from pydantic_ai_harness.memory import InMemoryStore, SqliteMemoryStore
from pydantic_ai_harness.tool_output_limits import Band, Truncate

from office import approval_email, brain, bus, config, db, dispatch, integrations, mock, plugins, procurement_workflow, speaker
from pydantic_ai.usage import UsageLimits
from office.deps import Deps

INSTRUCTIONS = """\
You are the office assistant, available in the dashboard and devices around the office.
You can discuss any topic and use registered integrations. The source board/location is context,
not a list of permitted topics. For status questions read office_status; never turn a question into a purchase.
For unavailable integrations, explain the limitation honestly; do not claim an action occurred.
People hand you tasks by voice. Your job is to make things happen with zero friction:
- If the request is clear enough, act immediately with your tools. Don't ask for confirmation;
  the system asks a human for approval when policy requires it.
- Keep the conversation coherent using message history. Resolve references like "yes", "there" and quantities from that history.
- Location is only input context. A cafe problem is about the cafe even when the source is the printer.
- Before advising on a meeting location, read_environment_history for that location and read_sensors for current conditions. In this demo the stage button uses board kitchen; do not assume other rooms share its readings.
- If earlier samples show poor meeting conditions, mention that history before proceeding. Label simulated evidence explicitly, distinguish past from current conditions, and never claim a meeting was booked without a confirmed booking result.
- Record requests about missing furniture or supplies outside the approved catalogue with create_ticket; do not force them into a printer/kitchen catalogue.
- Before purchasing, call prepare_purchase_request with the concrete need and appropriate configured location. This validates scope and prices, not approval.
- Ask a question only if you truly cannot act.
- Your final reply is spoken aloud and shown on a tiny screen: ONE short sentence, max ~20 words.
- Orders are simulated in this demo. When you order something, say it was ordered (simulated).
- Never invent products or prices; use the catalogue tools.
- Follow the company brain's procedures (lookup_procedure) when deciding how to handle something.
- Keep the office notebook (memory) for durable facts about this office and its people's preferences only.
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
# The office notebook persists across tasks and restarts (live mode). Mock mode stays offline and stateless.
notebook = Memory(
    SqliteMemoryStore(database=config.MEMORY_PATH) if config.LIVE else InMemoryStore(), heading="Office notebook"
)
# Notion pages can be huge: clamp any single tool result before it reaches the model.
limits = ToolOutputLimits(bands=[Band(over=12_000, action=Truncate(max_chars=12_000))])
office_agent = Agent(
    model,
    deps_type=Deps,
    output_type=[str, DeferredToolRequests],
    instructions=INSTRUCTIONS,
    # ApprovalEmail emails the routed approver when a run ends awaiting approval; it is not a model tool.
    capabilities=[speaker.SpeakerContext(), approval_email.ApprovalEmail(), progress, limits, notebook],
)


@office_agent.instructions
async def context(ctx: RunContext[Deps]) -> str:
    b = ctx.deps.board
    others = ", ".join(f"{x.id} ({x.location}: {', '.join(sorted(x.modules))})" for x in config.BOARDS.values())
    return (
        f"Now: {datetime.now().strftime('%A %Y-%m-%d %H:%M')}.\n"
        "Who is speaking is given separately (speaker). The server applies office policy and severity.\n"
        f"You are at board '{b.id}' in the {b.location}. Equipment here: {', '.join(b.equipment)}. "
        f"Responsible team: {b.responsible_team}.\n"
        f"Configured locations (context, not topic restrictions): {others}.\n"
        f"Office policy: orders above {config.OFFICE['max_order_nok']} NOK always need a human; {'; '.join(config.OFFICE['preferences'])}.\n"
        f"Company brain ({'Notion snapshot' if brain.source() == brain.SNAPSHOT else 'local copy'}): {'; '.join(brain.headings())}. "
        f"Approval routing, applied by the server: {', '.join(f'{k} → {v}' for k, v in brain.routing().items())}.\n"
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
    cart_attempt = procurement_workflow.oda_attempt(conn, task_id)
    if cart_attempt:
        value = procurement_workflow.get_context(conn, task_id)
        grocery = value.get("grocery") if value else None
        if cart_attempt["state"] == "confirmed":
            if isinstance(grocery, dict):
                grocery.update(state="cart_ready", cart_url=cart_attempt["cart_url"] or grocery.get("cart_url"))
                procurement_workflow.store_context(conn, task_id, value)
            return _update(conn, task_id, status="checkout_ready", pending="[]",
                           reply="Oda cart ready. Review delivery and pay on Oda; no order has been placed yet.")
        # Keep the exact basket and pending snapshot: later approval can only perform
        # Oda's read-only reconciliation, never replay an additive cart mutation.
        if isinstance(grocery, dict):
            grocery["state"] = "uncertain"
            procurement_workflow.store_context(conn, task_id, value)
        conn.execute("UPDATE oda_cart_attempts SET state='uncertain',updated_at=? WHERE task_id=?", (db.now(), task_id))
        return _update(conn, task_id, status="failed",
                       reply="Oda cart update was interrupted. Check Oda to reconcile it; these products will not be added again.")
    attempt = db.row(conn, "SELECT state, reference FROM procurement_attempts WHERE task_id = ?", task_id)
    if attempt and attempt["state"] == "confirmed":
        reply = f"Ordered (simulated), ref {attempt['reference']}."
        return _update(conn, task_id, status="done", reply=reply, pending="[]")
    if not attempt:
        procurement_workflow.release_quote(conn, task_id)
    return _update(conn, task_id, status="failed", reply=reply, pending="[]")


def recover_interrupted(conn) -> None:
    """Runs don't survive a restart, so close any left running instead of showing them as live forever."""
    registered_checks = db.row(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='facility_device_inputs'")
    for task in db.rows(conn, "SELECT id FROM tasks WHERE status = 'running'"):
        if registered_checks and db.row(conn, """SELECT 1 FROM device_events d
            JOIN facility_device_inputs f ON f.id=d.event_id
            WHERE d.task_id=? AND json_extract(f.body,'$.type')='stock_low'""", task["id"]):
            # The facility handler resumes a persisted check without a model and
            # stops uncertain supplier attempts for explicit reconciliation.
            continue
        _close_failed_run(conn, task["id"], "This was interrupted by a restart. Please send it again.")


def start_task(conn, board_id: str, who: "speaker.Speaker | str", text: str) -> str:
    """Create a new input task; explicit transcript endpoints continue clarification.

    `who` is the request's speaker annotation, or a plain label (e.g. "sensor") for non-speech sources.
    """
    task_id = db.new_id()
    person_name = who.person.name if isinstance(who, speaker.Speaker) else who
    conn.execute(
        "INSERT INTO tasks (id, board_id, person, prompt, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'running', ?, ?)",
        (task_id, board_id, person_name, text, db.now(), db.now()),
    )
    if isinstance(who, speaker.Speaker):
        speaker.save(conn, task_id, who)
    return task_id


async def run(conn, task_id: str, prompt: str | None, deferred: DeferredToolResults | None = None,
              *, chat_history=None, rooms=None, event_id=None, conversation_id=None) -> dict:
    t = db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id)
    board = config.BOARDS[t["board_id"]]
    who = speaker.load(conn, task_id, t["person"])  # persisted, so a resumed approval keeps the same speaker
    deps = Deps(board=board, person=who.person, task_id=task_id, db=conn, speaker=who)
    history = chat_history if chat_history is not None else ModelMessagesTypeAdapter.validate_json(t["messages"])
    _update(conn, task_id, status="running")

    try:
        if config.MODEL == "mock" and prompt is not None and chat_history is None:
            from office import environment_queries
            environment = environment_queries.answer(conn, task_id, prompt)
            if environment is not None:
                return _update(conn, task_id, status="done", reply=environment["reply"]) | {
                    "environment": environment["environment"], "trace": environment["trace"],
                    "command": db.set_command(conn, board.id, environment["reply"], "green")}
        if config.MODEL != "mock" and prompt is not None:
            # Live mode: Claude converses with read tools and submits validated requests (office/conversation.py).
            from office import conversation
            return await conversation.run(conn, task_id, prompt, rooms=rooms,
                                          event_id=event_id, conversation_id=conversation_id, chat_history=chat_history)
        if chat_history is not None:
            prepared = {"interpretation": {"intent": "conversation", "item_id": None, "quantity": 1,
                "summary": prompt[:500], "question": None, "assessment": {"severity": "routine", "confidence": 1,
                "requires_confirmation": False, "reason": "Conversation; any purchasing requires a separately validated quote"}},
                "proposal": None, "approval_required": True, "research": []}
            procurement_workflow.store_context(conn, task_id, prepared)
        else:
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
            toolsets=plugins.for_board(board) + integrations.toolsets(),
            message_history=history or None,
            deferred_tool_results=deferred,
            usage_limits=UsageLimits(request_limit=6, tool_calls_limit=10, total_tokens_limit=16000),
        )
    except Exception as e:  # Surface failure without undoing a staged action.
        if config.MODEL != "mock":
            current = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
            room = rooms._load(task_id) if rooms else None
            if room and room.get("message_id"):
                bus.publish("conversation.failed", {"task_id": task_id, "error": type(e).__name__})
                return rooms._update(task_id, room, room["state"], room["reply"])
            if current["status"] != "running":
                from office import conversation
                bus.publish("conversation.failed", {"task_id": task_id, "error": type(e).__name__})
                return conversation.task_result(conn, task_id, rooms)
        task = _close_failed_run(conn, task_id, f"Sorry, something went wrong: {type(e).__name__}")
        bus.publish("board.say", {"board_id": board.id, "text": task["reply"]})
        color = "green" if task["status"] in {"done", "checkout_ready"} else "red"
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
    if approve and conn.execute("SELECT 1 FROM sqlite_master WHERE name='office_work_links'").fetchone():
        linked = db.row(conn, "SELECT w.body FROM office_work_links l JOIN office_work_items w ON w.id=l.work_id WHERE l.source_id=?", "task:" + task_id)
        if linked and json.loads(linked["body"])["status"] in {"completed", "cancelled", "failed"}:
            raise StaleApproval("Linked work has ended; create a new request before approving")
    prepared = procurement_workflow.get_context(conn, task_id)
    if prepared and prepared.get("source") == "oda-food":
        from office.oda_food import service
        try:
            return await service(conn).decide(task_id, approve, proposal_fingerprint)
        except procurement_workflow.ProcurementConflict as error:
            raise StaleApproval(str(error)) from error
    if prepared and prepared.get("source") == "validated-need":
        try:
            return await dispatch.decide(conn, task_id, approve, proposal_fingerprint)
        except procurement_workflow.ProcurementConflict as error:
            raise StaleApproval(str(error)) from error
    if prepared and prepared.get("source") == "facility-rule":
        from office.facility_api import decide_replenishment
        try:
            return await decide_replenishment(conn, task_id, approve, proposal_fingerprint)
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
