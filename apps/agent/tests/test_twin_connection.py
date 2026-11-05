"""The TypeScript twin and the physical bridge share one durable Python workflow."""
from uuid import uuid4

from fastapi.testclient import TestClient
from office.api import create_app
from office_procurement.serialization import proposal_fingerprint


def event(kind="button_pressed", payload=None):
    return {"event_id": str(uuid4()), "station_id": "kitchen", "type": kind,
            "timestamp": "2026-11-05T14:00:00Z", "payload": payload or {}}


def test_button_is_a_durable_dashboard_request_and_retry_returns_current_state():
    with TestClient(create_app(":memory:")) as client:
        pressed = event()
        first = client.post("/api/device-events", json=pressed)
        assert first.status_code == 200, first.text
        request = first.json()["request"]
        assert request["station_id"] == "kitchen"
        assert request["status"] == "awaiting_input"
        assert client.get("/api/requests").json()[0]["id"] == request["id"]
        supplied = client.post(f"/api/requests/{request['id']}/transcript", json={"text": "We need coffee"})
        assert supplied.status_code == 200, supplied.text
        assert supplied.json()["request"]["status"] == "awaiting_approval"
        retry = client.post("/api/device-events", json=pressed).json()
        assert retry["duplicate"] is True
        assert retry["request"]["id"] == request["id"]
        assert retry["request"]["status"] == "awaiting_approval"
        assert len(client.get("/api/state").json()["tasks"]) == 1


def test_twin_approval_fingerprint_and_native_delivery_share_python_state():
    with TestClient(create_app(":memory:")) as client:
        incoming = client.post("/api/device-events", json=event("speech_transcript", {"text": "We need coffee"}))
        assert incoming.status_code == 200, incoming.text
        request = incoming.json()["request"]
        assert request["status"] == "awaiting_approval"
        assert request["proposal"]["simulation"] is True
        task_id = request["id"]
        assert client.post(f"/api/requests/{task_id}/approve", json={"proposal_fingerprint": "changed"}).status_code == 409
        assert client.get("/api/state").json()["orders"] == []
        reviewed = {"proposal_fingerprint": proposal_fingerprint(request["proposal"])}
        approved = client.post(f"/api/requests/{task_id}/approve", json=reviewed)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "ordered"
        assert approved.json()["supplier_reference"].startswith("SIM-")
        repeated = client.post(f"/api/requests/{task_id}/approve", json=reviewed)
        assert repeated.status_code == 200, repeated.text
        state = client.get("/api/state").json()
        assert len(state["orders"]) == 1
        assert client.post(f"/api/orders/{state['orders'][0]['id']}/delivered").status_code == 200
        displayed = client.get("/api/requests").json()[0]
        assert displayed["id"] == task_id and displayed["status"] == "resolved"
