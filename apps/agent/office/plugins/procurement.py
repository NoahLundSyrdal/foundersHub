"""Read-only sourcing and exact-quote simulated execution behind server approval."""
from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.toolsets import FunctionToolset
from office import db
from office.deps import Deps
from office import procurement_workflow as workflow
from office_procurement.serialization import proposal_fingerprint
from office_procurement.planning import needs_approval

requires: set[str] = set()
toolset = FunctionToolset()


@toolset.tool
async def search_catalogue(ctx: RunContext[Deps], query: str) -> list[dict]:
    """Search station-approved items; the prepared quote contains compared supplier offers."""
    stations, products, _, _ = workflow.current_configuration()
    station = next((s for s in stations if s["id"] == ctx.deps.board.id), None)
    if not station:
        return []
    words = query.lower().split()
    value = workflow.get_context(ctx.deps.db, ctx.deps.task_id) or {}
    return [{**p, "prepared_quote": value.get("proposal") if (value.get("proposal") or {}).get("product", {}).get("id") == p["id"] else None}
            for p in products if p["station_id"] == station["id"] and p["id"] in station["item_ids"] and
            (not words or any(w in f"{p['id']} {p['name']}".lower() for w in words))]


def execute_order(ctx: RunContext[Deps], product_id: str, quantity: int = 1) -> str:
    """Execute the exact prepared quote in simulation; human review follows office and severity policy."""
    conn, task_id = ctx.deps.db, ctx.deps.task_id
    workflow.ensure_schema(conn)
    existing = db.row(conn, "SELECT * FROM orders WHERE tool_call_id=?", ctx.tool_call_id)
    if existing:
        return f"Already ordered (simulated), ref {existing['ref']}."
    value = workflow.get_context(conn, task_id)
    if not value:
        return "Refused: assessment and sourcing must run before ordering."
    if value.get("duplicate_task_id"):
        return f"Not ordering again: an equivalent request is already active ({value['duplicate_task_id']})."
    if not value.get("proposal"):
        return "Refused: " + (value.get("question") or value["interpretation"].get("question") or "No eligible supplier quote is available.")
    proposal = value["proposal"]
    if type(quantity) is not int or not 1 <= quantity <= 20 or product_id != proposal["product"]["id"] or quantity != proposal["quantity"]:
        return "Refused: tool arguments differ from the assessed need and exact prepared quote."
    try:
        value, rules = workflow.validate_quote(conn, task_id)
    except workflow.ProcurementConflict as error:
        return f"Refused: {error}"
    opened = db.row(conn, "SELECT * FROM orders WHERE product_id=? AND status='simulated'", product_id)
    if opened:
        return f"Not ordering again: this supply or service already has simulated order {opened['ref']}."
    if db.row(conn, "SELECT * FROM procurement_attempts WHERE task_id=?", task_id):
        return "Refused: execution was already attempted; reconcile its persisted outcome before retrying."
    # Office policy authorizes automation; an unverified speaker name cannot grant it.
    requires_human = needs_approval({"assessment": value["assessment"], "proposal": proposal}, rules)
    if requires_human and not value["approval_required"]:
        value["approval_required"] = True
        workflow.store_context(conn, task_id, value)
    if requires_human and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata=workflow.approval_metadata(value, ctx.deps.person.name))
    if requires_human and value.get("approved_fingerprint") != proposal_fingerprint(proposal):
        return "Refused: exact quote approval has not been recorded."
    conn.execute("INSERT INTO procurement_attempts VALUES(?,?,?,'started',NULL)",
                 (task_id, ctx.tool_call_id, proposal_fingerprint(proposal)))
    ref = f"SIM-{db.new_id()[:8].upper()}"
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,'simulated',?,?)",
                     (db.new_id(), task_id, ctx.tool_call_id, product_id, quantity, proposal["total_nok"], ref, db.now()))
        conn.execute("UPDATE procurement_attempts SET state='confirmed',reference=? WHERE task_id=?", (ref, task_id))
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.execute("UPDATE procurement_attempts SET state='uncertain' WHERE task_id=?", (task_id,))
        return "Execution outcome uncertain. Do not retry; reconcile the persisted execution identifier."
    return f"Ordered (SIMULATED) {quantity} × {proposal['product']['name']} for {proposal['total_nok']:g} NOK, ref {ref}, delivery {proposal['product']['delivery_estimate']}."


@toolset.tool
async def place_order(ctx: RunContext[Deps], product_id: str, quantity: int = 1) -> str:
    """Execute the exact prepared quote in simulation after server policy and approval checks."""
    return execute_order(ctx, product_id, quantity)


@toolset.tool
async def prepare_purchase_request(ctx: RunContext[Deps], text: str, board_id: str) -> dict:
    """Prepare a validated quote for an explicitly requested purchase. No execution or approval. Resolve the need from conversation before calling."""
    from office import config
    if board_id not in config.BOARDS or not text.strip() or len(text) > 2000:
        return {"error": "Provide a configured location and a concrete purchase need"}
    old = workflow.get_context(ctx.deps.db, ctx.deps.task_id) or {}
    if old.get("proposal") or old.get("grocery") or workflow.execution_attempted(ctx.deps.db, ctx.deps.task_id):
        return {"error": "A quote or execution already exists; use its existing review controls"}
    ctx.deps.db.execute("UPDATE tasks SET board_id=? WHERE id=?", (board_id, ctx.deps.task_id))
    ctx.deps.board = config.BOARDS[board_id]
    try:
        return await workflow.prepare_need(ctx.deps.db, ctx.deps.task_id, text)
    except workflow.ProcurementConflict as error:
        return {"error": str(error)}
