"""Office operations: maintenance tickets and awareness of what's going on."""

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from office import db
from office.deps import Deps

requires: set[str] = set()
toolset = FunctionToolset()


@toolset.tool
async def create_ticket(ctx: RunContext[Deps], summary: str, team: str | None = None) -> str:
    """Report something broken or needing attention. Defaults to the responsible team at this board."""
    team = team or ctx.deps.board.responsible_team
    ctx.deps.db.execute(
        "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (db.new_id(), ctx.deps.task_id, ctx.deps.board.id, summary, team, db.now()),
    )
    return f"Ticket created for {team} (local record; nobody has been contacted)."


@toolset.tool
async def office_status(ctx: RunContext[Deps]) -> dict:
    """What's currently open in the office: open orders and tickets."""
    return {
        "open_orders": db.rows(ctx.deps.db, "SELECT product_id, quantity, ref, created_at FROM orders WHERE status = 'simulated'"),
        "open_tickets": db.rows(ctx.deps.db, "SELECT board_id, summary, team, created_at FROM tickets WHERE status = 'open'"),
    }
