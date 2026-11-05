"""Readings from this board's sensors. Only enabled on boards with a temperature/humidity module."""

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from office import db
from office.deps import Deps

requires = {"temperature_humidity"}
toolset = FunctionToolset()


@toolset.tool
def read_sensors(ctx: RunContext[Deps]) -> list[dict]:
    """Latest reading of every sensor on this board (temperature °C, humidity %, stock_level %, ...)."""
    return db.rows(
        ctx.deps.db,
        "SELECT kind, value, MAX(at) AS at FROM sensor_readings WHERE board_id = ? GROUP BY kind",
        ctx.deps.board.id,
    )
