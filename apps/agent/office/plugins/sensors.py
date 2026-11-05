"""Read-only physical telemetry available from every conversation location."""
from datetime import datetime, timezone
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from office import config, db
from office.deps import Deps

requires = set()
toolset = FunctionToolset()

@toolset.tool
async def read_sensors(ctx: RunContext[Deps], board_id: str | None = None) -> list[dict]:
    """Read physical sensors, including temperature C, CO2 ppm and humidity percent.
    Without board_id, return all configured boards with their source locations.
    Never describe stale readings as current or infer a room from the microphone location.
    No readings means telemetry is unavailable, not normal conditions.
    """
    if board_id is not None and board_id not in config.BOARDS:
        raise ValueError("Unknown board")
    rows = db.rows(ctx.deps.db,
        "SELECT board_id, kind, value, at FROM (SELECT *, ROW_NUMBER() OVER "
        "(PARTITION BY board_id, kind ORDER BY at DESC, rowid DESC) AS rank "
        "FROM sensor_readings) WHERE rank = 1")
    now = datetime.now(timezone.utc)
    result = []
    for row in rows:
        if row["board_id"] not in config.BOARDS or (board_id and row["board_id"] != board_id):
            continue
        try:
            age = (now - datetime.fromisoformat(row["at"].replace("Z", "+00:00"))).total_seconds()
            fresh = -5 <= age < 30
        except (ValueError, TypeError):
            fresh = False
        result.append({**row, "location": config.BOARDS[row["board_id"]].location,
                       "fresh": fresh, "simulation": False})
    return result
