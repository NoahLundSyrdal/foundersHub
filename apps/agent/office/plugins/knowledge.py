"""The company brain (Notion): procedures and who approves what. See office/brain.py."""

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from office import brain
from office.deps import Deps

requires: set[str] = set()
toolset = FunctionToolset()


@toolset.tool
async def lookup_procedure(ctx: RunContext[Deps], question: str) -> list[dict] | str:
    """Search the company brain (office procedures) for how this office handles something, e.g. 'broken equipment'."""
    return brain.search(question) or "Nothing in the company brain about that."


@toolset.tool
async def who_approves(ctx: RunContext[Deps]) -> dict[str, str]:
    """The approval routing table: approval type → the person who approves it. The system applies it automatically."""
    return brain.routing()
