"""Fixed sensor rules dispatch local actions and approved orders without model calls."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from office import agent, config, procurement_workflow
from office.api import create_app
from office_procurement.serialization import proposal_fingerprint


@pytest.fixture
def facility(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Deterministic checks must not call a model")
    monkeypatch.setattr(procurement_workflow, "run_worker", forbidden)
    monkeypatch.setattr(agent.office_agent, "run", forbidden)
    monkeypatch.setenv("OFFICE_WHATSAPP_ENABLED", "false")
    monkeypatch.setitem(config.OFFICE, "require_approval", True)
    app = create_app(":memory:")
    clock = [datetime.now(UTC)]
    app.state.facilities.engine.clock = lambda: clock[0]
    with TestClient(app) as client:
        yield client, clock


def configure(client, rule_id, **changes):
    rule = next(r for r in client.get("/api/facility").json()["rules"] if r["id"] == rule_id)
    response = client.post(f"/api/facility/rules/{rule_id}", json={**rule, **changes})
    assert response.status_code == 200, response.text
    return response.json()


def sample(client, clock, metric, value, simulation=True, event_id=None):
    clock[0] += timedelta(seconds=1)
    body = {"event_id": event_id or uuid4().hex, "board_id": "kitchen", "metric": metric,
            "value": value, "timestamp": clock[0].isoformat(), "simulation": simulation}
    response = client.post("/api/facility/readings", json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def test_co2_alert_ticket_recovery_and_duplicate_event(facility):
    client, clock = facility
    configure(client, "co2-high", min_samples=1, min_duration_seconds=0)
    result, body = sample(client, clock, "co2_ppm", 1400)
    incident = result["incidents"][0]
    assert incident["action_results"]["alert"]["status"] == "visible"
    assert incident["action_results"]["ticket"]["local_only"] is True
    assert len(client.get("/api/state").json()["tickets"]) == 1
    assert client.post("/api/facility/readings", json=body).json()["duplicate"] is True
    assert sample(client, clock, "co2_ppm", 1500)[0]["incidents"] == []
    assert len(client.get("/api/state").json()["tickets"]) == 1
    ack = client.post(f"/api/facility/incidents/{incident['id']}/acknowledge", json={}).json()
    assert ack["status"] == "active" and ack["acknowledged_at"]
    recovered = sample(client, clock, "co2_ppm", 900)[0]["incidents"][0]
    assert recovered["status"] == "recovered"
    assert client.get("/api/state").json()["orders"] == []


def test_replenishment_and_exact_approval_both_avoid_models(facility):
    client, clock = facility
    configure(client, "stock-low", min_samples=1, min_duration_seconds=0)
    result, body = sample(client, clock, "stock_level", 10)
    action = result["incidents"][0]["action_results"]["replenish"]
    assert action["status"] == "awaiting_approval"
    request = next(r for r in client.get("/api/requests").json() if r["id"] == action["task_id"])
    assert request["proposal"]["quantity"] == 1
    url = f"/api/requests/{request['id']}/approve"
    assert client.post(url, json={"proposal_fingerprint": "wrong"}).status_code == 409
    reviewed = {"proposal_fingerprint": proposal_fingerprint(request["proposal"])}
    approved = client.post(url, json=reviewed)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "ordered"
    assert approved.json()["supplier_reference"].startswith("SIM-")
    latest = client.get("/api/facility").json()["incidents"][0]
    assert latest["action_results"]["replenish"]["status"] == "done"
    assert client.post(url, json=reviewed).status_code == 200
    assert client.post("/api/facility/readings", json=body).json()["duplicate"] is True
    assert len(client.get("/api/state").json()["orders"]) == 1


def test_live_stock_uses_policy_and_recovery_cannot_duplicate_purchase(facility, monkeypatch):
    client, clock = facility
    monkeypatch.setitem(config.OFFICE, "require_approval", False)
    configure(client, "stock-low", min_samples=1, min_duration_seconds=0)
    first, _ = sample(client, clock, "stock_level", 10, simulation=False)
    assert first["incidents"][0]["action_results"]["replenish"]["status"] == "done"
    sample(client, clock, "stock_level", 80, simulation=False)
    second, _ = sample(client, clock, "stock_level", 10, simulation=False)
    assert second["incidents"][0]["action_results"]["replenish"]["status"] == "duplicate"
    assert len(client.get("/api/state").json()["orders"]) == 1


@pytest.mark.parametrize("rule_id,metric,value,action", [
    ("temperature-high", "temperature_c", 32, "ticket"),
    ("temperature-low", "temperature_c", 10, "ticket"),
    ("humidity-high", "humidity_pct", 85, "ticket"),
    ("bin-full", "bin_fill_pct", 95, "ticket"),
    ("obstruction", "obstruction_mm", 50, "sound"),
    ("noise-high", "noise_level", 20000, "alert"),
    ("equipment-tilted", "tilt_deg", 80, "alert"),
])
def test_each_sensor_feature_dispatches_its_configured_actions(facility, rule_id, metric, value, action):
    client, clock = facility
    configure(client, rule_id, enabled=True, min_samples=1, min_duration_seconds=0)
    result, _ = sample(client, clock, metric, value)
    assert result["incidents"][0]["action_results"][action]["status"] in {"visible", "created", "pending"}
    assert client.get("/api/state").json()["orders"] == []


def test_alias_telemetry_uses_rules_and_simulation_never_enters_live_tools(facility):
    client, clock = facility
    configure(client, "co2-high", min_samples=1, min_duration_seconds=0)
    sample(client, clock, "co2_ppm", 1600)
    assert client.get("/api/air-quality").json()["level"] == "unknown"
    clock[0] += timedelta(seconds=1)
    body = {"key": "co2_ppm", "value": 900, "event_id": "physical-co2", "timestamp": clock[0].isoformat()}
    result = client.post("/api/telemetry", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["accepted"] is True
    assert client.get("/api/air-quality").json()["readings"]["co2_ppm"] == 900
    assert client.post("/api/telemetry", json={"key": "dist_min_distance", "value": 100}).json()["accepted"] is False
    conn = client.app.state.conn
    assert conn.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0] == 1
    clock[0] += timedelta(seconds=31)
    assert client.get("/api/air-quality").json()["level"] == "unknown"


def test_disabled_rule_cannot_fall_through_to_legacy_interpretation(facility):
    client, clock = facility
    configure(client, "stock-low", enabled=False)
    response = client.post("/api/sensor", json={"board_id": "kitchen", "kind": "stock_level", "value": 5,
                           "timestamp": clock[0].isoformat(), "event_id": "disabled-stock"})
    assert response.status_code == 200, response.text
    assert response.json()["incidents"] == []
    assert client.get("/api/state").json()["tasks"] == []


def test_validation_rejects_unsafe_mappings_and_reused_payloads(facility):
    client, clock = facility
    co2 = next(r for r in client.get("/api/facility").json()["rules"] if r["id"] == "co2-high")
    assert client.post("/api/facility/rules/co2-high", json={**co2, "actions": ["replenish"], "item_id": "coffee"}).status_code == 422
    stock = next(r for r in client.get("/api/facility").json()["rules"] if r["id"] == "stock-low")
    assert client.post("/api/facility/rules/stock-low", json={**stock, "item_id": "toner"}).status_code == 400
    _, body = sample(client, clock, "co2_ppm", 800)
    assert client.post("/api/facility/readings", json={**body, "value": 1600}).status_code == 400
    assert client.post("/api/facility/readings", json={**body, "event_id": "bad-range", "value": 0}).status_code == 422


@pytest.mark.parametrize("board_id,item_id", [("kitchen", "coffee"), ("bathroom", "toilet-paper"), ("printer", "printer-paper")])
def test_completed_stock_checks_dispatch_without_a_model_or_browser(facility, board_id, item_id):
    client, clock = facility
    body = {"event_id": uuid4().hex, "station_id": board_id, "type": "stock_low", "timestamp": clock[0].isoformat(),
            "payload": {"item_id": item_id, "level": 10}}
    response = client.post("/api/device-events", json=body)
    assert response.status_code == 200, response.text
    request = response.json()["request"]
    assert request["proposal"]["product"]["id"] == item_id
    assert request["proposal"]["quantity"] == 1
    assert request["status"] == "awaiting_approval"
    approved = client.post(f"/api/requests/{request['id']}/approve", json={"proposal_fingerprint": proposal_fingerprint(request["proposal"])})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "ordered"
    assert client.post("/api/device-events", json=body).json()["duplicate"] is True
    assert len(client.get("/api/state").json()["orders"]) == 1
    changed = {**body, "payload": {**body["payload"], "level": 5}}
    assert client.post("/api/device-events", json=changed).status_code == 409


def test_wrong_station_or_service_cannot_be_replenished_from_a_check(facility):
    client, clock = facility
    for item in ("toner", "ventilation-service", "unknown"):
        body = {"event_id": uuid4().hex, "station_id": "kitchen", "type": "stock_low", "timestamp": clock[0].isoformat(), "payload": {"item_id": item}}
        assert client.post("/api/device-events", json=body).status_code == 400
    assert client.get("/api/state").json()["tasks"] == []


def test_bathroom_numeric_stock_check_never_uses_legacy_prompt(facility):
    client, clock = facility
    response = client.post("/api/sensor", json={"board_id": "bathroom", "kind": "stock_level", "value": 10,
                           "timestamp": clock[0].isoformat(), "event_id": "bathroom-numeric"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "awaiting_approval"
    assert response.json()["proposal"]["product"]["id"] == "toilet-paper"


def test_sound_is_a_claimed_backend_command_with_acknowledged_outcome(facility):
    client, clock = facility
    configure(client, "obstruction", enabled=True, min_samples=1, min_duration_seconds=0)
    result, _ = sample(client, clock, "obstruction_mm", 50)
    incident = result["incidents"][0]
    action_id = incident["action_results"]["sound"]["action_id"]
    base = "/api/devices/kitchen/actions"
    assert client.get(base).json()["actions"][0]["state"] == "pending"
    claim = client.post(base + "/claim", json={"consumer_id": "office-station-bridge"})
    assert claim.status_code == 200, claim.text
    assert claim.json()["actions"][0]["id"] == action_id
    assert claim.json()["actions"][0]["state"] == "dispatched"
    assert client.post(base + "/claim", json={"consumer_id": "other-consumer"}).json()["actions"] == []
    acknowledgement = {"status": "delivered", "detail": "Station adapter confirmed playback"}
    assert client.post(base + f"/{action_id}/acknowledge", json=acknowledgement).status_code == 200
    assert client.post(base + f"/{action_id}/acknowledge", json=acknowledgement).status_code == 200
    current = next(i for i in client.get("/api/facility").json()["incidents"] if i["id"] == incident["id"])
    assert current["action_results"]["sound"]["status"] == "delivered"


def test_recovery_cancels_an_unclaimed_sound_without_a_browser(facility):
    client, clock = facility
    configure(client, "obstruction", enabled=True, min_samples=1, min_duration_seconds=0)
    sample(client, clock, "obstruction_mm", 50)
    sample(client, clock, "obstruction_mm", 500)
    actions = client.get("/api/devices/kitchen/actions").json()["actions"]
    assert actions[0]["state"] == "cancelled"
    assert client.post("/api/devices/kitchen/actions/claim", json={"consumer_id": "bridge"}).json()["actions"] == []


def test_interrupted_stock_registration_resumes_in_backend_tick(facility):
    client, clock = facility
    from office import db
    service, conn = client.app.state.facilities, client.app.state.conn
    task_id = agent.start_task(conn, "kitchen", "sensor", "Stock check: coffee")
    event = {"event_id": "registered-before-crash", "station_id": "kitchen", "type": "stock_low",
             "timestamp": clock[0].isoformat(), "payload": {"item_id": "coffee"}}
    conn.execute("INSERT INTO device_events VALUES(?,?,?)", (event["event_id"], task_id, "{}"))
    service.remember_device_input(event["event_id"], event)
    service.tick()
    assert db.row(conn, "SELECT status FROM tasks WHERE id=?", task_id)["status"] == "awaiting_approval"
    assert procurement_workflow.get_context(conn, task_id)["proposal"]["quantity"] == 1
    service.tick()
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_interrupted_supplier_attempt_is_never_automatically_reexecuted(facility, monkeypatch):
    client, clock = facility
    from office import db
    from office.plugins import procurement
    body = {"event_id": "uncertain-check", "station_id": "kitchen", "type": "stock_low",
            "timestamp": clock[0].isoformat(), "payload": {"item_id": "coffee"}}
    request = client.post("/api/device-events", json=body).json()["request"]
    conn = client.app.state.conn
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (request["id"],))
    conn.execute("INSERT INTO procurement_attempts VALUES(?,?,?,'uncertain',NULL)",
                 (request["id"], "uncertain-call", proposal_fingerprint(request["proposal"])))
    def forbidden(*args, **kwargs):
        raise AssertionError("An uncertain supplier attempt must not execute again")
    monkeypatch.setattr(procurement, "place_order", forbidden)
    client.app.state.facilities.tick()
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", request["id"])
    assert task["status"] == "failed" and "reconcile" in task["reply"]
    assert client.get("/api/state").json()["orders"] == []


def test_backend_check_events_are_scoped_without_a_dashboard(facility):
    client, clock = facility
    from office import bus
    service = client.app.state.facilities
    transitions = []
    def capture(event, data):
        if event == "facility.updated":
            transitions.extend(i["status"] for i in service.engine.snapshot()["incidents"] if i["rule_id"] == "co2-high")
    unsubscribe = bus.add_listener(capture, source=client.app.state.conn)
    try:
        configure(client, "co2-high", min_samples=1, min_duration_seconds=0)
        sample(client, clock, "co2_ppm", 1400)
        sample(client, clock, "co2_ppm", 900)
    finally:
        unsubscribe()
    assert "active" in transitions and "recovered" in transitions

@pytest.mark.parametrize("key,value,metric", [
    ("co2_co2", 987, "co2_ppm"),
    ("co2_temperature", 29.8, "temperature_c"),
    ("co2_humidity", 21.4, "humidity_pct"),
    ("aht20_temperature", 29.0, "temperature_c"),
    ("aht20_humidity", 24.0, "humidity_pct"),
])
def test_observed_firmware_quantity_aliases(facility, key, value, metric):
    client, clock = facility
    response = client.post("/api/telemetry", json={"key": key, "value": value, "timestamp": clock[0].isoformat(), "event_id": uuid4().hex})
    assert response.status_code == 200, response.text
    assert response.json()["metric"] == metric
