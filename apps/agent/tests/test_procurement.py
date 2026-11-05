import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from office import config, procurement_workflow
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
