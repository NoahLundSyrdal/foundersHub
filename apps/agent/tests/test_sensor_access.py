from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
import asyncio
from office import config, db, plugins
from office.plugins import sensors

def test_general_chat_reads_physical_sources_with_freshness():
    conn = db.connect(":memory:")
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO sensor_readings VALUES (?,?,?,?)",
                 ("kitchen", "co2_ppm", 800, now.isoformat()))
    conn.execute("INSERT INTO sensor_readings VALUES (?,?,?,?)",
                 ("kitchen", "temperature_c", 22, (now - timedelta(minutes=2)).isoformat()))
    ctx = SimpleNamespace(deps=SimpleNamespace(db=conn, board=config.BOARDS["office"]))
    assert sensors.toolset in plugins.for_board(config.BOARDS["office"])
    result = asyncio.run(sensors.read_sensors(ctx))
    assert next(r for r in result if r["kind"] == "co2_ppm")["fresh"]
    assert not next(r for r in result if r["kind"] == "temperature_c")["fresh"]
    assert all(r["location"] == "Kitchen" and not r["simulation"] for r in result)
    assert asyncio.run(sensors.read_sensors(ctx, "bathroom")) == []
    with pytest.raises(ValueError):
        asyncio.run(sensors.read_sensors(ctx, "unknown"))
