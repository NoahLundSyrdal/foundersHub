"""Output on any board in the office: show a message / colour on its screen or light grid."""

from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from office import bus, config, db
from office.deps import Deps

requires: set[str] = set()
toolset = FunctionToolset()


@toolset.tool
async def show_message(ctx: RunContext[Deps], board_id: str, text: str, color: Literal["blue", "amber", "green", "red"] = "blue") -> str:
    """Show a short message on another board's screen/lights, e.g. to tell the kitchen something."""
    board = config.BOARDS.get(board_id)
    if not board:
        return f"No board {board_id!r}."
    if not board.modules & {"screen", "light_grid"}:
        return f"Board {board_id} has no screen or lights."
    bus.publish("board.say", {"board_id": board_id, "text": text})
    db.set_command(ctx.deps.db, board_id, text, color)
    return f"Shown on {board.location} board."
