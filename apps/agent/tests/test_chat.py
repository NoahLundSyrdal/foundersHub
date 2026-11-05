from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart, ToolReturnPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from office import agent
from office.api import create_app


def test_followup_keeps_history_after_restart_and_retries_do_not_duplicate(tmp_path):
    path = str(tmp_path / "chat.sqlite")
    def model(messages, info):
        if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            return ModelResponse(parts=[TextPart("Recorded the cafe napkins report")])
        prompts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
        if any(isinstance(p, str) and "yes, two packs" in p for p in prompts):
            assert any(isinstance(p, str) and "Napkins are missing in the cafe" in p for p in prompts)
            return ModelResponse(parts=[ToolCallPart("create_ticket", {"summary": "Cafe needs two packs of napkins", "team": "Office team"})])
        return ModelResponse(parts=[TextPart("Should I record the cafe napkins request, and how many packs?")])
    with agent.office_agent.override(model=FunctionModel(model)):
        with TestClient(create_app(path)) as client:
            first = client.post("/api/chat", json={"conversation_id":"demo", "event_id":"first", "board_id":"printer", "text":"Napkins are missing in the cafe"})
            assert first.status_code == 200, first.text
        with TestClient(create_app(path)) as client:
            payload = {"conversation_id":"demo", "event_id":"second", "board_id":"printer", "text":"yes, two packs"}
            second = client.post("/api/chat", json=payload)
            assert second.status_code == 200, second.text
            assert second.json()["request"]["status"] == "maintenance_open", second.text
            assert second.json()["request"]["report_location_text"] == "Cafe needs two packs of napkins"
            assert client.post("/api/chat", json=payload).json()["duplicate"]
            assert len(client.get("/api/state").json()["tickets"]) == 1
            assert len(client.get("/api/chat/demo").json()["requests"]) == 2
            assert client.get("/api/chat/other").json()["requests"] == []
            assert client.post("/api/chat", json={**payload,"text":"different"}).status_code == 409
