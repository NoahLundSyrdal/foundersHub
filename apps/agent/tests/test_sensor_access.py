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


def test_bridge_samples_update_dashboard_and_agent_from_same_source():
    from fastapi.testclient import TestClient
    from office.api import create_app
    from office import bus
    app = create_app(":memory:")
    conn = app.state.facilities.conn
    events = []
    unsubscribe = bus.add_listener(lambda name, data: events.append(name), source=conn)
    try:
        with TestClient(app) as client:
            for kind, value in [("temperature", 22.5), ("humidity", 45.0), ("co2_ppm", 650.0)]:
                response = client.post("/api/sensor", json={"board_id": "kitchen", "kind": kind, "value": value})
                assert response.status_code == 200, response.text
            snapshot = client.get("/api/facility").json()
            expected = {"temperature_c": 22.5, "humidity_pct": 45.0, "co2_ppm": 650.0}
            dashboard = {r["metric"]: r["value"] for r in snapshot["readings"] if r["board_id"] == "kitchen"}
            assert dashboard == expected
            ctx = SimpleNamespace(deps=SimpleNamespace(db=conn, board=config.BOARDS["office"]))
            readings = asyncio.run(sensors.read_sensors(ctx))
            assert {r["kind"]: r["value"] for r in readings} == expected
            assert all(r["fresh"] for r in readings)
            assert events.count("facility.updated") >= 3
    finally:
        unsubscribe()
