"""Room conversations use the normal task/device API and real bridge receipts."""
from datetime import datetime, timedelta
import json
import time
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from office import config, db
from office.api import create_app
from office.room_messaging import DraftDecision
from office.room_service import RoomQuery


@pytest.fixture
def room_app(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_WHATSAPP_ENABLED", "false")
    monkeypatch.setenv("ODA_FOOD_ENABLED", "false")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setattr(config, "MODEL", "mock")
    monkeypatch.setitem(config.OFFICE, "require_approval", True)
    start = (datetime.now(ZoneInfo("Europe/Oslo")) + timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0)
    calendar = {"rooms": [{"name": "Test room", "simulation": True}],
                "bookings": [{"id": "test-booking", "room_name": "Test room", "starts_at": start.isoformat(),
                              "ends_at": (start + timedelta(hours=1)).isoformat(), "owner_id": "test-owner", "revision": 1, "simulation": True}],
                "contacts": [{"id": "test-owner", "name": "Jürgen", "whatsapp_number": "+12025550123"}]}
    calendar_path = tmp_path / "calendar.json"
    calendar_path.write_text(json.dumps(calendar))
    token_path = tmp_path / "private-whatsapp" / "bridge-token"
    calls = {"parser": [], "composer": []}
    async def parser(context):
        calls["parser"].append(context)
        return RoomQuery(action="check_room", room_name="Test room", starts_at=start,
                         ends_at=start + timedelta(minutes=30), reply="Checking the requested meeting interval.")
    async def composer(context):
        calls["composer"].append(context)
        return DraftDecision(action="message_owner", reply="Message ready.",
            body=f"Hi {context['owner']['name']}, {context['requester_name']} would like Test room tomorrow. Can you discuss the booking?")
    app = create_app(":memory:", room_calendar_path=calendar_path, bridge_token_path=token_path,
                     room_parser=parser, room_composer=composer)
    now = [time.time()]
    app.state.whatsapp.clock = lambda: now[0]
    with TestClient(app) as client:
        yield {"client": client, "app": app, "calls": calls, "calendar": calendar, "calendar_path": calendar_path,
               "headers": {"Authorization": "Bearer " + token_path.read_text()}, "now": now}


def input_(runtime, text, event_id, conversation_id=None, board="kitchen"):
    body = {"board_id": board, "text": text, "event_id": event_id, "person": "Alex"}
    if conversation_id:
        body["conversation_id"] = conversation_id
    return runtime["client"].post("/api/input", json=body)


def conflict(runtime):
    response = input_(runtime, "Check the meeting room Test room tomorrow at 13:00", "room-initial")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["room_workflow"]["state"] == "conflict"
    assert "Jürgen" in result["reply"]
    return result


def queued(runtime):
    initial = conflict(runtime)
    response = input_(runtime, "Send him a message", "room-followup", initial["conversation_id"])
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["id"] == initial["id"]
    assert result["room_workflow"]["state"] == "queued"
    return result


def heartbeat(runtime):
    response = runtime["client"].post("/api/whatsapp/heartbeat", headers=runtime["headers"],
                                      json={"consumer_id": "test-bridge", "connected": True})
    assert response.status_code == 200, response.text


def claim(runtime):
    response = runtime["client"].post("/api/whatsapp/jobs/claim", headers=runtime["headers"], json={"consumer_id": "test-bridge"})
    assert response.status_code == 200, response.text
    return response.json()["jobs"]


def view(runtime, task_id):
    response = runtime["client"].get(f"/api/room-conversations/{task_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_conflict_composition_queue_and_receipts_update_one_task_requests_and_board(room_app):
    runtime = room_app
    initial = queued(runtime)
    task_id = initial["id"]
    assert [step["stage"] for step in initial["trace"]] == ["checking", "checking_conditions", "checking_history", "conflict", "composing", "queued"]
    request = next(r for r in runtime["client"].get("/api/requests").json() if r["id"] == task_id)
    assert request["trace"] == initial["trace"]
    assert request["room_workflow"]["recipient_name"] == "Jürgen"
    assert request["room_workflow"]["message_body"].startswith("Office assistant test (simulated booking)\nHi Jürgen")
    assert len(runtime["calls"]["parser"]) == len(runtime["calls"]["composer"]) == 1
    assert "whatsapp_number" not in json.dumps(runtime["calls"]["composer"])
    heartbeat(runtime)
    jobs = claim(runtime)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["message_id"] == initial["room_workflow"]["message_id"]
    assert job["phone"] == "+12025550123"
    assert claim(runtime) == []
    for state in ("sent", "delivered", "read"):
        receipt = runtime["client"].post(f"/api/whatsapp/jobs/{job['execution_id']}/receipt", headers=runtime["headers"],
            json={"consumer_id": "test-bridge", "status": state, "provider_reference": "test-provider-message"})
        assert receipt.status_code == 200, receipt.text
        current = view(runtime, task_id)
        assert current["id"] == task_id and current["room_workflow"]["state"] == state
        projected = next(r for r in runtime["client"].get("/api/requests").json() if r["id"] == task_id)
        assert projected["room_workflow"]["state"] == state
        command = runtime["client"].get("/api/devices/kitchen/command").json()["command"]
        assert command["text"] == current["reply"][:120]
    state = runtime["client"].get("/api/state").json()
    assert len(state["tasks"]) == 1 and state["orders"] == []
    assert {step["stage"] for step in view(runtime, task_id)["trace"]} >= {"checking", "conflict", "composing", "queued", "sent", "delivered", "read"}


def test_retry_returns_current_conversation_without_recomposing_or_requeueing(room_app):
    runtime = room_app
    result = queued(runtime)
    heartbeat(runtime)
    job = claim(runtime)[0]
    runtime["client"].post(f"/api/whatsapp/jobs/{job['execution_id']}/receipt", headers=runtime["headers"],
        json={"consumer_id": "test-bridge", "status": "delivered", "provider_reference": "test-provider-message"})
    retry = input_(runtime, "Send him a message", "room-followup", result["conversation_id"])
    assert retry.status_code == 200
    assert retry.json()["room_workflow"]["state"] == "delivered"
    initial_retry = input_(runtime, "Check the meeting room Test room tomorrow at 13:00", "room-initial")
    assert initial_retry.json()["id"] == result["id"]
    assert initial_retry.json()["room_workflow"]["state"] == "delivered"
    conn = runtime["app"].state.conn
    assert db.row(conn, "SELECT COUNT(*) AS count FROM room_message_drafts")["count"] == 1
    assert db.row(conn, "SELECT COUNT(*) AS count FROM whatsapp_delivery_jobs")["count"] == 1
    assert len(runtime["calls"]["parser"]) == len(runtime["calls"]["composer"]) == 1
    assert input_(runtime, "Tell him a different message", "room-followup", result["conversation_id"]).status_code == 409
    assert input_(runtime, "Send him a message", "different-station", result["conversation_id"], board="bathroom").status_code == 409


def test_device_speech_transcripts_reuse_the_room_conversation_and_retry_safely(room_app):
    runtime = room_app
    def event(event_id, text):
        return {"event_id": event_id, "station_id": "kitchen", "type": "speech_transcript",
                "timestamp": "2026-11-05T12:00:00Z", "payload": {"text": text}}
    initial_body = event("speech-room-initial", "Check meeting room Test room tomorrow")
    first = runtime["client"].post("/api/device-events", json=initial_body)
    assert first.status_code == 200, first.text
    task_id = first.json()["request"]["id"]
    assert first.json()["request"]["room_workflow"]["state"] == "conflict"
    followup_body = event("speech-room-followup", "Send him a message")
    second = runtime["client"].post("/api/device-events", json=followup_body)
    assert second.status_code == 200, second.text
    assert second.json()["request"]["id"] == task_id
    assert second.json()["request"]["room_workflow"]["state"] == "queued"
    retried = runtime["client"].post("/api/device-events", json=followup_body)
    assert retried.json()["duplicate"] is True
    assert retried.json()["request"]["id"] == task_id
    assert len(runtime["client"].get("/api/state").json()["tasks"]) == 1
    assert len(runtime["calls"]["composer"]) == 1


def test_button_and_legacy_transcript_continue_same_room_task(room_app):
    runtime = room_app
    pressed = runtime["client"].post("/api/device-events", json={"event_id": "button-room", "station_id": "kitchen",
        "type": "button_pressed", "timestamp": "2026-11-05T12:00:00Z", "payload": {}})
    assert pressed.status_code == 200
    task_id = pressed.json()["request"]["id"]
    first = runtime["client"].post(f"/api/requests/{task_id}/transcript", json={"text": "Check the meeting room Test room tomorrow", "event_id": "legacy-initial"})
    assert first.status_code == 200, first.text
    assert first.json()["request"]["room_workflow"]["state"] == "conflict"
    second = runtime["client"].post(f"/api/requests/{task_id}/transcript", json={"text": "Send him a message", "event_id": "legacy-followup"})
    assert second.status_code == 200, second.text
    assert second.json()["request"]["id"] == task_id
    assert second.json()["request"]["room_workflow"]["state"] == "queued"
    assert len(runtime["client"].get("/api/state").json()["tasks"]) == 1


def test_unrelated_coffee_stays_on_procurement_path(room_app):
    runtime = room_app
    initial = conflict(runtime)
    result = input_(runtime, "We need coffee", "coffee-request")
    assert result.status_code == 200, result.text
    assert result.json()["id"] != initial["id"]
    assert result.json()["status"] == "awaiting_approval"
    assert result.json()["proposal"]["product"]["id"] == "coffee"
    assert "room_workflow" not in result.json()
    assert len(runtime["calls"]["parser"]) == 1 and runtime["calls"]["composer"] == []
    assert runtime["client"].get("/api/state").json()["orders"] == []


def test_offline_queue_never_claims_sent_and_expires_without_delivery(room_app):
    runtime = room_app
    result = queued(runtime)
    assert "queued" in result["reply"].lower()
    assert "sent" not in result["reply"].lower()
    assert runtime["client"].post("/api/whatsapp/jobs/claim", headers=runtime["headers"], json={"consumer_id": "test-bridge"}).status_code == 409
    runtime["now"][0] += 121
    status = runtime["client"].get("/api/whatsapp/status").json()
    assert status["queued"] == 0 and status["connected"] is False
    expired = view(runtime, result["id"])
    assert expired["room_workflow"]["state"] == "expired"
    assert "expired" in expired["reply"].lower()
    assert "sent" not in expired["reply"].lower()
    heartbeat(runtime)
    assert claim(runtime) == []


def test_cancellation_before_claim_prevents_outbound_message(room_app):
    runtime = room_app
    result = queued(runtime)
    cancelled = input_(runtime, "Cancel it", "cancel-room-message", result["conversation_id"])
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["room_workflow"]["state"] == "cancelled"
    heartbeat(runtime)
    assert claim(runtime) == []
    assert view(runtime, result["id"])["room_workflow"]["state"] == "cancelled"
    assert db.row(runtime["app"].state.conn, "SELECT state FROM whatsapp_delivery_jobs")["state"] == "skipped"


def test_booking_changed_before_claim_skips_old_message_and_updates_task(room_app):
    runtime = room_app
    result = queued(runtime)
    runtime["calendar"]["bookings"][0]["revision"] = 2
    runtime["calendar_path"].write_text(json.dumps(runtime["calendar"]))
    heartbeat(runtime)
    assert claim(runtime) == []
    current = view(runtime, result["id"])
    assert current["room_workflow"]["state"] == "skipped"
    assert "changed" in current["reply"].lower() and "not sent" in current["reply"].lower()
    assert db.row(runtime["app"].state.conn, "SELECT state FROM whatsapp_delivery_jobs")["state"] == "skipped"
