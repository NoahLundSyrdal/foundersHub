"""Location context does not limit conversation or registered integration tools."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import RunContext
from office.deps import Deps
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from office import agent, config, plugins, procurement_workflow
from office.api import create_app


@pytest.fixture
def api():
    with TestClient(create_app(":memory:")) as client:
        yield client


@pytest.mark.parametrize("board", ["office", "kitchen", "bathroom", "printer"])
@pytest.mark.parametrize("text", ["Help plan onboarding for our new colleague", "What is the status of my coffee order?", "Help with next week's team meeting"])
def test_conversation_is_not_limited_to_selected_area(api, board, text):
    response = api.post("/api/input", json={"board_id": board, "text": text})
    assert response.status_code == 200
    task = response.json()
    assert task["status"] == "done"
    assert task["proposal"] is None
    view = next(r for r in api.get("/api/requests").json() if r["id"] == task["id"])
    assert view["intent"] == "conversation"
    assert view["status"] == "resolved"
    assert api.get("/api/state").json()["orders"] == []
    assert api.get("/api/work").json()["items"] == []


def test_general_is_default_and_new_board_does_not_need_procurement_station(api, monkeypatch):
    assert api.post("/api/input", json={"text": "Help plan onboarding"}).json()["board_id"] == "office"
    board = config.Board(id="meeting-room", location="Meeting room", responsible_team="People", equipment=[], modules=set())
    monkeypatch.setitem(config.BOARDS, board.id, board)
    task = api.post("/api/input", json={"board_id": board.id, "text": "Plan onboarding"}).json()
    assert task["status"] == "done"
    assert procurement_workflow.get_context(api.app.state.conn, task["id"])["interpretation"]["intent"] == "conversation"
    assert api.post("/api/input", json={"board_id": "unknown", "text": "Hello"}).status_code == 404


def test_other_integration_reaches_agent_from_any_location(api, monkeypatch):
    tools = FunctionToolset()
    calls = []
    @tools.tool
    async def onboarding_status(ctx: RunContext[Deps]) -> str:
        calls.append(True)
        return "Two onboarding tasks remain"
    monkeypatch.setattr(plugins, "PLUGINS", [*plugins.PLUGINS, SimpleNamespace(toolset=tools, requires=set())])
    def model(messages, info):
        if any(isinstance(part, ToolReturnPart) for part in messages[-1].parts):
            return ModelResponse(parts=[TextPart("Two onboarding tasks remain")])
        assert "onboarding_status" in {tool.name for tool in info.function_tools}
        return ModelResponse(parts=[ToolCallPart("onboarding_status", {})])
    with agent.office_agent.override(model=FunctionModel(model)):
        task = api.post("/api/input", json={"board_id": "bathroom", "text": "What is the onboarding status?"}).json()
    assert calls == [True], task["reply"]
    assert task["reply"] == "Two onboarding tasks remain"
    assert task["proposal"] is None


def test_status_question_does_not_duplicate_or_reject_pending_order(api):
    purchase = api.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"}).json()
    question = api.post("/api/input", json={"board_id": "kitchen", "text": "What is the status of coffee?"}).json()
    assert question["status"] == "done"
    assert "Not ordering again" not in question["reply"]
    assert question["proposal"] is None
    assert purchase["id"] in question["reply"]
    tasks = api.get("/api/state").json()["tasks"]
    assert next(t for t in tasks if t["id"] == purchase["id"])["status"] == "awaiting_approval"
