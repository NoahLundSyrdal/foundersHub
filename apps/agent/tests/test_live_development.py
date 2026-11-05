"""Software requests in normal conversation become reviewable drafts, never paid jobs or releases."""
import json

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from office import config, conversation, db
from office.api import create_app
from office.development import GitHub


@pytest.fixture
def live_development(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL", "anthropic:test")
    monkeypatch.setenv("OFFICE_DEVELOPMENT_ENABLED", "true")
    monkeypatch.setenv("ODA_FOOD_ENABLED", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calendar = tmp_path / "rooms.json"
    calendar.write_text(json.dumps({"rooms": [], "bookings": [], "contacts": []}))
    async def forbidden(*args, **kwargs):
        raise AssertionError("Conversation cannot dispatch or query GitHub")
    monkeypatch.setattr(GitHub, "request", forbidden)
    app = create_app(":memory:", room_calendar_path=calendar, bridge_token_path=tmp_path / "token")
    with TestClient(app) as client:
        yield client, app


def output(info, text):
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"reply": text, "needs_details": False})])


@pytest.mark.parametrize("path", ["input", "speech", "button_transcript"])
def test_software_feature_becomes_one_local_draft_without_purchase_or_dispatch(live_development, path):
    client, app = live_development
    feedback = "Add monitoring for a temperature drop of 3 degrees within 10 minutes."
    calls = []
    def model(messages, info):
        calls.append(1)
        names = {tool.name for tool in info.function_tools}
        assert "propose_development_change" in names
        assert not names.intersection({"send_development_request", "merge", "deploy", "place_order"})
        results = [part.content for message in messages for part in message.parts
                   if isinstance(part, ToolReturnPart) and part.tool_name == "propose_development_change"]
        if not results:
            return ModelResponse(parts=[ToolCallPart("propose_development_change", {"requested_change": feedback})])
        assert results[-1]["state"] == "draft"
        assert results[-1]["cloud_run_started"] is False
        assert results[-1]["installed"] is False
        return output(info, "I saved the feature request for review in Improve. It has not been implemented yet.")
    with conversation.assistant.override(model=FunctionModel(model)):
        if path == "input":
            payload = {"board_id": "kitchen", "text": feedback, "event_id": "feature-input"}
            first = client.post("/api/input", json=payload)
            replay = client.post("/api/input", json=payload)
        else:
            event = {"event_id": "feature-event", "station_id": "kitchen", "timestamp": db.now(),
                     "type": "speech_transcript" if path == "speech" else "button_pressed",
                     "payload": {"text": feedback} if path == "speech" else {}}
            first = client.post("/api/device-events", json=event)
            if path == "speech":
                replay = client.post("/api/device-events", json=event)
            else:
                task_id = first.json()["request"]["id"]
                payload = {"text": feedback, "event_id": "feature-transcript"}
                first = client.post(f"/api/requests/{task_id}/transcript", json=payload)
                replay = client.post(f"/api/requests/{task_id}/transcript", json=payload)
    assert first.status_code == replay.status_code == 200
    drafts = client.get("/api/development").json()["requests"]
    assert len(drafts) == 1 and drafts[0]["text"] == feedback and drafts[0]["state"] == "draft"
    assert drafts[0]["run_id"] is None
    assert app.state.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert app.state.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    assert len(calls) == 2


def test_ordinary_conversation_does_not_create_development_request(live_development):
    client, _ = live_development
    with conversation.assistant.override(model=FunctionModel(lambda messages, info: output(info, "We can discuss room comfort."))):
        response = client.post("/api/input", json={"board_id": "kitchen", "text": "Why does temperature matter?"})
    assert response.status_code == 200
    assert client.get("/api/development").json()["requests"] == []


def test_voice_draft_does_not_reset_an_active_dispatch(live_development):
    client, app = live_development
    item = app.state.development.draft("already-dispatching", "Fix display")
    app.state.development._update(item["id"], state="dispatching")
    def model(messages, info):
        if not any(isinstance(part, ToolReturnPart) and part.tool_name == "propose_development_change"
                   for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("propose_development_change", {"requested_change": "Add a sensor chart"})])
        return output(info, "Saved a draft for review.")
    with conversation.assistant.override(model=FunctionModel(model)):
        assert client.post("/api/input", json={"board_id": "kitchen", "text": "Add a sensor chart"}).status_code == 200
    assert app.state.development.get(item["id"])["state"] == "dispatching"
