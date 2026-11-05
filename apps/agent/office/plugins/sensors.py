"""Read-only physical telemetry available from every conversation location."""
from datetime import datetime, timezone
import json
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


@toolset.tool
async def read_environment_history(ctx: RunContext[Deps], board_id: str | None = None) -> list[dict]:
    """Read up to 24 environmental samples from the past seven days for one board.
    Defaults to the conversation board. The demo stage button is mapped to kitchen.
    Includes explicitly labelled simulation fixtures. These are historical evidence,
    never current conditions; simulations do not establish real-world conditions.
    """
    board_id = board_id or ctx.deps.board.id
    if board_id not in config.BOARDS:
        raise ValueError("Unknown board")
    if not ctx.deps.db.execute("SELECT 1 FROM sqlite_master WHERE name='facility_events'").fetchone():
        return []
    rows = db.rows(ctx.deps.db, "SELECT body FROM facility_events "
        "WHERE json_extract(body, '$.board_id')=? "
        "AND json_extract(body, '$.metric') IN ('co2_ppm','temperature_c','humidity_pct') "
        "AND julianday(json_extract(body, '$.timestamp')) BETWEEN julianday('now', '-7 days') AND julianday('now') "
        "ORDER BY julianday(json_extract(body, '$.timestamp')) DESC, id LIMIT 24", board_id)
    return [{**json.loads(row['body']), "location": config.BOARDS[board_id].location,
             "historical": True} for row in rows]
