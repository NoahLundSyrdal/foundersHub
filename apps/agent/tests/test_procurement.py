import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from office import agent, config, procurement_workflow
from office.api import create_app
from office_procurement.serialization import proposal_fingerprint


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setitem(config.OFFICE, "require_approval", True)
    with TestClient(create_app(":memory:")) as api:
        yield api


def test_button_order_and_exact_approval_on_python_backend(client):
    event = {"event_id": "button-test", "station_id": "kitchen", "type": "button_pressed", "timestamp": "2026-11-05T15:00:00Z", "payload": {}}
    request = client.post("/api/device-events", json=event).json()["request"]
    assert request["status"] == "awaiting_input"
    assert client.post("/api/device-events", json=event).json()["duplicate"]
    result = client.post(f"/api/requests/{request['id']}/transcript", json={"text": "We need 2 coffee"}).json()["request"]
    proposal = result["proposal"]
    assert result["status"] == "awaiting_approval"
    assert len(proposal["sourcing"]["alternatives"]) == 3
    assert proposal["quantity"] == 2
    url = f"/api/requests/{request['id']}/approve"
    assert client.post(url, json={"proposal_fingerprint": "wrong"}).status_code == 409
    body = {"proposal_fingerprint": proposal_fingerprint(proposal)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json=body), range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert len(client.get("/api/state").json()["orders"]) == 1
    assert client.get("/api/requests").json()[0]["status"] == "ordered"


def test_repairs_and_critical_alerts_do_not_auto_spend(client, monkeypatch):
    monkeypatch.setitem(config.OFFICE, "require_approval", False)
    repair = client.post("/api/input", json={"board_id": "bathroom", "text": "Book a plumber", "person": "Leo"}).json()
    assert repair["status"] == "awaiting_approval"
    assert repair["assessment"]["requires_confirmation"]
    assert repair["proposal"]["fulfillment_kind"] == "service"
    critical = client.post("/api/input", json={"board_id": "kitchen", "text": "Smoke reported near the coffee machine"}).json()
    assert critical["assessment"]["severity"] == "critical"
    alerts = client.get("/api/alerts").json()
    assert len(alerts) == 2
    assert client.post(f"/api/alerts/{alerts[0]['id']}/acknowledge", json={}).status_code == 200
    assert client.get("/api/state").json()["orders"] == []



def test_policy_allows_automatic_routine_orders_without_identity(client, monkeypatch):
    monkeypatch.setitem(config.OFFICE, "require_approval", False)
    result = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    assert result["status"] == "done"
    assert result["approval_required"] is False
    assert len(client.get("/api/state").json()["orders"]) == 1


def test_changed_delivery_and_expired_quotes_are_rejected(client):
    task = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    conn = client.app.state.conn
    original = procurement_workflow.get_context(conn, task["id"])
    changed = json.loads(json.dumps(original))
    changed["proposal"]["expires_at"] = "2020-01-01T00:00:00.000Z"
    procurement_workflow.store_context(conn, task["id"], changed)
    assert client.post(f"/api/tasks/{task['id']}/decide", json={"approve": True}).status_code == 409
    changed = json.loads(json.dumps(original))
    chosen = next(x for x in changed["proposal"]["sourcing"]["alternatives"] if x["offer"]["id"] == changed["proposal"]["offer_id"])
    chosen["offer"]["delivery_hours"] += 1
    procurement_workflow.store_context(conn, task["id"], changed)
    assert client.post(f"/api/tasks/{task['id']}/decide", json={"approve": True}).status_code == 409
    assert client.get("/api/state").json()["orders"] == []

    current = client.get("/api/requests").json()[0]
    old = proposal_fingerprint(current["proposal"])
    refreshed = client.post(f"/api/requests/{task['id']}/requote", json={"proposal_fingerprint": old})
    assert refreshed.status_code == 200
    assert refreshed.json()["proposal"]["quote_revision"] == 1
    assert client.post(f"/api/requests/{task['id']}/approve", json={"proposal_fingerprint": old}).status_code == 409


def request_view(client, task_id):
    return next(r for r in client.get("/api/requests").json() if r["id"] == task_id)


def fail_model(monkeypatch):
    async def stopped(*args, **kwargs):
        raise RuntimeError("provider stopped")
    monkeypatch.setattr(agent.office_agent, "run", stopped)


def test_model_failure_before_ordering_frees_the_item(client, monkeypatch):
    real_run = agent.office_agent.run
    fail_model(monkeypatch)
    task = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    request = request_view(client, task["id"])
    assert request["status"] == "failed"
    assert request["proposal"] is None
    assert client.get("/api/state").json()["orders"] == []

    monkeypatch.setattr(agent.office_agent, "run", real_run)
    again = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    assert again["status"] == "awaiting_approval"


def test_model_failure_after_approval_without_execution_orders_nothing(client, monkeypatch):
    task = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    fail_model(monkeypatch)
    assert client.post(f"/api/tasks/{task['id']}/decide", json={"approve": True}).json()["status"] == "failed"
    request = request_view(client, task["id"])
    assert request["proposal"] is None
    assert client.get("/api/state").json()["orders"] == []


def test_model_failure_keeps_an_attempted_order_uncertain(client, monkeypatch):
    task = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    conn = client.app.state.conn
    conn.execute("INSERT INTO procurement_attempts VALUES(?,?,?,'started',NULL)", (task["id"], "call-1", "{}"))
    fail_model(monkeypatch)
    client.post(f"/api/requests/{task['id']}/approve", json={"proposal_fingerprint": proposal_fingerprint(task["proposal"])})
    request = request_view(client, task["id"])
    assert request["status"] == "failed"
    assert request["proposal"] is not None
    blocked = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    assert "Not ordering again" in blocked["reply"]


def restart_with(path, prepare):
    with TestClient(create_app(str(path))) as first:
        task = first.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
        prepare(first.app.state.conn, task["id"])
        first.app.state.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task["id"],))
    return TestClient(create_app(str(path))), task["id"]


def test_restart_closes_an_interrupted_run_and_frees_the_item(tmp_path):
    client, task_id = restart_with(tmp_path / "office.sqlite", lambda conn, task: None)
    with client:
        request = request_view(client, task_id)
        assert request["status"] == "failed"
        assert request["proposal"] is None
        again = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
        assert again["status"] == "awaiting_approval"


def test_restart_keeps_an_interrupted_attempt_uncertain(tmp_path):
    def attempted(conn, task):
        conn.execute("INSERT INTO procurement_attempts VALUES(?,?,?,'started',NULL)", (task, "call-1", "{}"))
    client, task_id = restart_with(tmp_path / "office.sqlite", attempted)
    with client:
        request = request_view(client, task_id)
        assert request["status"] == "failed"
        assert request["proposal"] is not None


def test_restart_reports_a_confirmed_order_as_ordered(tmp_path):
    def confirmed(conn, task):
        conn.execute("INSERT INTO procurement_attempts VALUES(?,?,?,'confirmed','SIM-1')", (task, "call-1", "{}"))
        conn.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,'simulated',?,?)", ("order-1", task, "call-1", "coffee", 1, 252.02, "SIM-1", "2026-11-05T15:00:00Z"))
    client, task_id = restart_with(tmp_path / "office.sqlite", confirmed)
    with client:
        request = request_view(client, task_id)
        assert request["status"] == "ordered"
        assert request["supplier_reference"] == "SIM-1"


def test_critical_report_is_a_ticket_that_can_be_closed(client):
    task = client.post("/api/input", json={"board_id": "kitchen", "text": "Smoke reported near the coffee machine"}).json()
    request = next(r for r in client.get("/api/requests").json() if r["id"] == task["id"])
    assert request["status"] == "maintenance_open"
    assert request["intent"] == "maintenance"
    assert request["assigned_to"] == config.BOARDS["kitchen"].responsible_team
    assert client.get("/api/state").json()["orders"] == []
    assert client.post(f"/api/requests/{task['id']}/resolve").json()["status"] == "resolved"


def test_rejecting_an_expired_quote_frees_the_item(client):
    task = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    conn = client.app.state.conn
    expired = procurement_workflow.get_context(conn, task["id"])
    expired["proposal"]["expires_at"] = "2020-01-01T00:00:00.000Z"
    procurement_workflow.store_context(conn, task["id"], expired)

    blocked = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    assert blocked["status"] == "done" and "Not ordering again" in blocked["reply"]

    rejected = client.post(f"/api/requests/{task['id']}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "resolved"
    assert rejected.json()["supplier_reference"] is None
    assert client.post(f"/api/requests/{task['id']}/reject").status_code == 409

    again = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    assert again["status"] == "awaiting_approval"
    assert client.get("/api/state").json()["orders"] == []


def test_stock_pack_sizes_are_not_order_quantities(client):
    event = {"event_id": "stock-paper", "station_id": "bathroom", "type": "stock_low", "timestamp": "2026-11-05T15:00:00Z", "payload": {"item_id": "toilet-paper", "level": 15}}
    first = client.post("/api/device-events", json=event).json()
    assert first["request"]["proposal"]["quantity"] == 1
    assert client.post("/api/device-events", json=event).json()["request"]["id"] == first["request"]["id"]
