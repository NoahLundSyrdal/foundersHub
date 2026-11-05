"""Ordering from the approved catalogue. Execution is SIMULATED; prices come from the catalogue, never the model."""

import threading

from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.toolsets import FunctionToolset

from office import config, db
from office.deps import Deps

requires: set[str] = set()
toolset = FunctionToolset()
_order_lock = threading.Lock()  # sync tools run in worker threads; check+insert must be atomic across tasks


@toolset.tool
def search_catalogue(ctx: RunContext[Deps], query: str) -> list[dict]:
    """Search the approved office catalogue. Use an empty query to list everything."""
    words = query.lower().split()
    return [
        {k: p[k] for k in ("id", "name", "price_nok", "delivery_estimate", "supplier")}
        for p in config.PRODUCTS.values()
        if not words or any(w in f"{p['id']} {p['name']}".lower() for w in words)
    ]


@toolset.tool(sequential=True)  # parallel calls would all pass the duplicate check before any insert
def place_order(ctx: RunContext[Deps], product_id: str, quantity: int = 1) -> str:
    """Order a catalogue product. A human is asked automatically if the speaker's limit is exceeded."""
    product = config.PRODUCTS.get(product_id)
    if not product:
        return f"Unknown product {product_id!r}. Use search_catalogue first."
    if not 1 <= quantity <= 50:
        return "Quantity must be between 1 and 50."
    total = product["price_nok"] * quantity
    if total > config.OFFICE["max_order_nok"]:
        return f"Refused: {total} NOK exceeds the office limit of {config.OFFICE['max_order_nok']} NOK."

    with _order_lock:
        return _place(ctx, product, product_id, quantity, total)


def _place(ctx: RunContext[Deps], product: dict, product_id: str, quantity: int, total: int) -> str:
    open_order = db.row(ctx.deps.db, "SELECT * FROM orders WHERE product_id = ? AND status = 'simulated'", product_id)
    if open_order:
        return f"Not ordering again: {product['name']} is already on its way (ref {open_order['ref']})."

    if total > ctx.deps.person.auto_approve_nok and not ctx.tool_call_approved:
        raise ApprovalRequired(
            metadata={"summary": f"{quantity} × {product['name']} = {total} NOK", "total_nok": total, "requested_by": ctx.deps.person.name}
        )

    existing = db.row(ctx.deps.db, "SELECT * FROM orders WHERE tool_call_id = ?", ctx.tool_call_id)
    if existing:  # same tool call replayed: never buy twice
        return f"Already ordered (simulated), ref {existing['ref']}."
    ref = f"SIM-{db.new_id()[:8].upper()}"
    ctx.deps.db.execute(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, 'simulated', ?, ?)",
        (db.new_id(), ctx.deps.task_id, ctx.tool_call_id, product_id, quantity, total, ref, db.now()),
    )
    return f"Ordered (SIMULATED) {quantity} × {product['name']} for {total} NOK, ref {ref}, delivery {product['delivery_estimate']}."
