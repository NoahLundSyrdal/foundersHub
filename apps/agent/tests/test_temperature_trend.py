"""Read-only temperature trend from stored physical facility readings."""
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from office import agent, config, procurement_workflow, temperature_trend
from office.api import create_app


@pytest.fixture
def trend(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Temperature trend must not call a model")
    monkeypatch.setattr(procurement_workflow, "run_worker", forbidden)
    monkeypatch.setattr(agent.office_agent, "run", forbidden)
    monkeypatch.setenv("OFFICE_WHATSAPP_ENABLED", "false")
    app = create_app(":memory:")
    now = datetime.now(UTC).replace(microsecond=0)
    app.state.facilities.engine.clock = lambda: now
    with TestClient(app) as client:
        yield client, app.state.conn, now


def put(conn, now, seconds_ago, value, *, board="kitchen", metric="temperature_c", simulation=False, raw=None):
    event_id = uuid4().hex
    if raw is None:
        body = {"event_id": event_id, "board_id": board, "metric": metric, "value": value,
                "timestamp": (now - timedelta(seconds=seconds_ago)).isoformat(), "simulation": simulation}
        raw = json.dumps(body)
    conn.execute("INSERT INTO facility_events VALUES (?,?)", (event_id, raw))


def get(client, board="kitchen"):
    response = client.get(f"/api/temperature-trend/{board}")
    assert response.status_code == 200, response.text
    return response.json()


def counts(conn):
    return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("facility_events", "facility_incidents", "tickets", "orders")]


def test_detects_measured_drop_with_labels_and_no_side_effects(trend):
    client, conn, now = trend
    put(conn, now, 500, 24.0)
    put(conn, now, 300, 22.5)
    put(conn, now, 10, 20.5)
    before = counts(conn)
    result = get(client)
    assert result["available"] is True and result["drop_detected"] is True
    assert result["status"] == "drop_detected"
    assert result["delta_c"] == -3.5
    assert result["earliest"]["value_c"] == 24.0 and result["latest"]["value_c"] == 20.5
    assert result["earliest"]["timestamp"] < result["latest"]["timestamp"]
    assert result["unit"] == "°C" and result["simulation"] is False
    assert "physical" in result["source"] and "no background alerting" in result["alerting"]
    assert result["sample_count"] == 3
    assert counts(conn) == before


def test_exact_three_degree_drop_counts(trend):
    client, conn, now = trend
    put(conn, now, 200, 24.0)
    put(conn, now, 5, 21.0)
    result = get(client)
    assert result["delta_c"] == -3.0 and result["drop_detected"] is True


def test_stable_rising_and_small_fall_are_not_drops(trend):
    client, conn, now = trend
    put(conn, now, 400, 21.0)
    put(conn, now, 5, 21.2)
    result = get(client)
    assert result["status"] == "stable" and result["drop_detected"] is False and result["available"] is True
    put(conn, now, 2, 25.0)
    result = get(client)
    assert result["status"] == "rising" and result["delta_c"] == 4.0 and result["drop_detected"] is False
    put(conn, now, 1, 19.0)
    result = get(client)
    assert result["status"] == "falling" and result["delta_c"] == -2.0 and result["drop_detected"] is False


def test_insufficient_and_empty_history_are_unknown(trend):
    client, conn, now = trend
    result = get(client)
    assert result["available"] is False and result["status"] == "unknown"
    assert result["reason"].startswith("insufficient")
    assert result["delta_c"] is None and result["drop_detected"] is None and result["latest"] is None
    put(conn, now, 10, 22.0)
    put(conn, now, 10, 22.0)  # same instant repeated is not a second sample
    result = get(client)
    assert result["available"] is False and result["reason"].startswith("insufficient")
    assert result["sample_count"] == 1


def test_stale_latest_reading_is_unknown(trend):
    client, conn, now = trend
    put(conn, now, 500, 25.0)
    put(conn, now, 121, 20.0)
    result = get(client)
    assert result["available"] is False and result["reason"].startswith("stale")
    assert result["delta_c"] is None and result["drop_detected"] is None


def test_readings_outside_window_and_future_are_excluded(trend):
    client, conn, now = trend
    put(conn, now, 601, 30.0)   # older than ten minutes
    put(conn, now, -6, 10.0)    # more than five seconds in the future
    put(conn, now, 300, 22.0)
    put(conn, now, -4, 21.8)    # small clock skew is tolerated
    result = get(client)
    assert result["available"] is True and result["sample_count"] == 2
    assert result["earliest"]["value_c"] == 22.0 and result["latest"]["value_c"] == 21.8
    assert result["drop_detected"] is False


def test_only_future_readings_do_not_count(trend):
    client, conn, now = trend
    put(conn, now, -30, 25.0)
    put(conn, now, -60, 15.0)
    result = get(client)
    assert result["available"] is False and result["sample_count"] == 0


def test_malformed_and_invalid_values_are_ignored(trend):
    client, conn, now = trend
    ts = (now - timedelta(seconds=100)).isoformat()
    put(conn, now, 0, None, raw="not json")
    put(conn, now, 0, None, raw=json.dumps(["kitchen", "temperature_c"]))
    put(conn, now, 0, None, raw='{"board_id":"kitchen","metric":"temperature_c","value":NaN,'
                                f'"timestamp":"{ts}","simulation":false}}')
    put(conn, now, 0, None, raw='{"board_id":"kitchen","metric":"temperature_c","value":-Infinity,'
                                f'"timestamp":"{ts}","simulation":false}}')
    put(conn, now, 50, "18.0")
    put(conn, now, 51, True)
    put(conn, now, 52, 500.0)  # outside the sensor's plausible range
    put(conn, now, 0, None, raw=json.dumps({"board_id": "kitchen", "metric": "temperature_c", "value": 18.0,
                                            "timestamp": "2026-01-01T00:00:00", "simulation": False}))  # naive
    put(conn, now, 0, None, raw=json.dumps({"board_id": "kitchen", "metric": "temperature_c", "value": 18.0,
                                            "timestamp": "garbage", "simulation": False}))
    put(conn, now, 0, None, raw=json.dumps({"board_id": "kitchen", "metric": "temperature_c", "value": 18.0,
                                            "simulation": False}))
    result = get(client)
    assert result["available"] is False and result["sample_count"] == 0
    put(conn, now, 200, 23.0)
    put(conn, now, 3, 22.9)
    result = get(client)
    assert result["available"] is True and result["sample_count"] == 2 and result["drop_detected"] is False


def test_simulated_and_other_streams_are_excluded(trend):
    client, conn, now = trend
    put(conn, now, 300, 30.0, simulation=True)
    put(conn, now, 5, 10.0, simulation=True)
    put(conn, now, 250, 30.0, simulation="false")  # not strictly false
    put(conn, now, 0, None, raw=json.dumps({"board_id": "kitchen", "metric": "temperature_c", "value": 30.0,
                                            "timestamp": (now - timedelta(seconds=240)).isoformat()}))  # missing flag
    put(conn, now, 200, 40.0, metric="humidity_pct")
    other = next(b for b in config.BOARDS if b != "kitchen") if len(config.BOARDS) > 1 else None
    if other:
        put(conn, now, 220, 35.0, board=other)
    result = get(client)
    assert result["available"] is False and result["sample_count"] == 0
    put(conn, now, 100, 22.0)
    put(conn, now, 4, 21.5)
    result = get(client)
    assert result["available"] is True and result["drop_detected"] is False
    assert result["earliest"]["value_c"] == 22.0 and result["latest"]["value_c"] == 21.5


def test_conflicting_values_at_same_instant_are_ambiguous(trend):
    client, conn, now = trend
    put(conn, now, 300, 25.0)
    put(conn, now, 300, 15.0)
    put(conn, now, 5, 21.0)
    result = get(client)
    assert result["available"] is False and result["sample_count"] == 1


def test_ingested_physical_readings_feed_trend(trend):
    client, conn, now = trend
    for seconds_ago, value in ((60, 22.0), (30, 21.9)):
        body = {"event_id": uuid4().hex, "board_id": "kitchen", "metric": "temperature_c", "value": value,
                "timestamp": (now - timedelta(seconds=seconds_ago)).isoformat(), "simulation": False}
        assert client.post("/api/facility/readings", json=body).status_code == 200
    result = get(client)
    assert result["available"] is True and result["status"] == "stable"
    assert result["sample_count"] == 2


def test_unknown_board_returns_404(trend):
    client, _, _ = trend
    assert client.get("/api/temperature-trend/no-such-board").status_code == 404


def test_compute_is_pure_function_of_stored_rows(trend):
    _, conn, now = trend
    put(conn, now, 100, 24.0)
    put(conn, now, 1, 20.0)
    later = now + timedelta(seconds=200)
    assert temperature_trend.compute(conn, "kitchen", now)["drop_detected"] is True
    assert temperature_trend.compute(conn, "kitchen", later)["reason"].startswith("stale")
