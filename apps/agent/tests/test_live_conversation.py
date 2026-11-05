"""Exercise live routing with a model double; never use the keyword mock or suppliers."""
from datetime import UTC, datetime, timedelta
import json

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from office import config, conversation, db, environment_queries, procurement_workflow
from office import agent
from office.api import create_app


def reply(info, text, needs_details=False):
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name,
        {"reply": text, "needs_details": needs_details})])


def returned(messages, name):
    return [p.content for m in messages for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == name]


def assessment():
    return {"severity": "routine", "confidence": .95, "requires_confirmation": False,
            "reason": "Clear requested replenishment, subject to office policy."}


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL", "anthropic:test")
    monkeypatch.setenv("ODA_FOOD_ENABLED", "false")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OFFICE_WHATSAPP_ENABLED", "false")
    monkeypatch.setitem(config.OFFICE, "require_approval", True)
    start = datetime.now(UTC) + timedelta(days=1)
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"rooms": [{"name": "Fokus", "board_id": "kitchen", "simulation": True}],
                                     "bookings": [], "contacts": []}))
    app = create_app(":memory:", room_calendar_path=calendar, bridge_token_path=tmp_path / "token")
    async def forbidden(*args, **kwargs):
        raise AssertionError("Live conversation must not use the scripted/second interpretation path")
    def forbidden_sync(*args, **kwargs):
        raise AssertionError("Live conversation must not use scripted environment replies")
    monkeypatch.setattr(procurement_workflow, "run_worker", forbidden)
    monkeypatch.setattr(environment_queries, "answer", forbidden_sync)
    with TestClient(app) as client:
        yield client, app, start


def say(client, text, **extra):
    response = client.post("/api/input", json={"board_id": "kitchen", "text": text, **extra})
    assert response.status_code == 200, response.text
    assert response.json()["status"] != "failed", response.json()
    return response.json()


def test_open_conversation_and_followup_are_resolved_and_keep_history(live):
    client, app, _ = live
    calls = []
    def model(messages, info):
        calls.append(messages)
        assert "place_order" not in {t.name for t in info.function_tools}
        assert "create_ticket" not in {t.name for t in info.function_tools}
        return reply(info, "Vi kan finne en praktisk løsning sammen.")
    with conversation.assistant.override(model=FunctionModel(model)):
        first = say(client, "Hvordan kan vi gjøre hverdagen på kontoret enklere?", event_id="general")
        assert say(client, "Hvordan kan vi gjøre hverdagen på kontoret enklere?", event_id="general")["id"] == first["id"]
        view = client.get("/api/requests").json()[0]
        assert view["status"] == "resolved" and view["question"] is None
        followup = client.post(f"/api/requests/{first['id']}/transcript", json={"text": "Hva bør vi starte med?", "event_id": "follow"})
        assert followup.status_code == 200, followup.text
        assert followup.json()["request"]["status"] == "resolved"
    assert len(calls) == 2
    assert len([p for m in calls[-1] for p in m.parts if isinstance(p, UserPromptPart) and isinstance(p.content, str)]) == 2
    assert client.get("/api/state").json()["tickets"] == []
    assert client.get("/api/state").json()["orders"] == []
    assert client.post("/api/input", json={"board_id": "kitchen", "text": "different", "event_id": "general"}).status_code == 409


@pytest.mark.parametrize("path", ["input", "device", "transcript"])
def test_indirect_air_question_reads_real_facts_without_scripted_routing(live, path):
    client, app, _ = live
    for metric, value in (("temperature_c", 25), ("humidity_pct", 45), ("co2_ppm", 1300)):
        app.state.conn.execute("INSERT INTO sensor_readings VALUES(?,?,?,?)", ("kitchen", metric, value, db.now()))
    def model(messages, info):
        facts = returned(messages, "read_conditions")
        if not facts:
            return ModelResponse(parts=[ToolCallPart("read_conditions", {})])
        assert next(r for r in facts[-1]["current"]["readings"] if r["kind"] == "co2_ppm")["value"] == 1300
        return reply(info, "CO2 er 1300 ppm; luft gjerne ut.")
    with conversation.assistant.override(model=FunctionModel(model)):
        text = "Det kjennes innestengt her, hva tenker du?"
        if path == "input":
            result = say(client, text)
            task_id = result["id"]
        else:
            event = {"event_id": "speech", "station_id": "kitchen", "type": "speech_transcript" if path == "device" else "button_pressed",
                     "timestamp": db.now(), "payload": {"text": text} if path == "device" else {}}
            result = client.post("/api/device-events", json=event).json()
            task_id = result["request"]["id"]
            if path == "transcript":
                response = client.post(f"/api/requests/{task_id}/transcript", json={"text": text})
                assert response.status_code == 200, response.text
    view = next(r for r in client.get("/api/requests").json() if r["id"] == task_id)
    assert view["status"] == "resolved" and "1300" in view["summary"]
    assert view["environment"]["current"]["board_id"] == "kitchen"
    assert client.get("/api/state").json()["orders"] == []
    assert client.get("/api/state").json()["tickets"] == []


def test_claude_decision_uses_exact_quote_and_cannot_replace_it(live):
    client, app, _ = live
    calls = []
    decision = {"intent": "purchase", "item_id": "cups", "summary": "More cups", "quantity": 2,
                "question": None, "assessment": assessment()}
    def model(messages, info):
        calls.append(1)
        if not returned(messages, "submit_request"):
            return ModelResponse(parts=[ToolCallPart("submit_request", {"decision": decision})])
        return reply(info, "Forslag på to pakker kopper er klart til godkjenning; kjøpet er simulert.")
    with conversation.assistant.override(model=FunctionModel(model)):
        task = say(client, "Det begynner å bli tomt for noe å drikke kaffen av, skaff to pakker.")
        assert task["status"] == "awaiting_approval" and task["proposal"]["quantity"] == 2
        assert app.state.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert client.post(f"/api/requests/{task['id']}/transcript", json={"text": "make it 3"}).status_code == 409
        assert client.post(f"/api/tasks/{task['id']}/decide", json={"approve": True, "proposal_fingerprint": "wrong"}).status_code == 409
        approved = client.post(f"/api/tasks/{task['id']}/decide", json={"approve": True})
        assert approved.status_code == 200, approved.text
    assert len(calls) == 2  # approval never calls the model
    assert app.state.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_broken_equipment_gets_local_ticket_only_when_requested(live):
    client, app, _ = live
    decision = {"intent": "maintenance", "item_id": None, "summary": "Oppvaskmaskinen starter ikke", "quantity": 1,
                "question": None, "assessment": {**assessment(), "severity": "urgent", "requires_confirmation": True}}
    def model(messages, info):
        if not returned(messages, "submit_request"):
            return ModelResponse(parts=[ToolCallPart("submit_request", {"decision": decision})])
        return reply(info, "Jeg har registrert feilen lokalt; ingen reparatør er kontaktet.")
    with conversation.assistant.override(model=FunctionModel(model)):
        task = say(client, "Oppvaskmaskinen vil ikke starte; kan du registrere at den trenger hjelp?")
    view = next(r for r in client.get("/api/requests").json() if r["id"] == task["id"])
    assert view["status"] == "maintenance_open"
    assert app.state.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


def test_room_tool_accepts_unlisted_phrasing_and_topic_can_change(live):
    client, app, start = live
    def model(messages, info):
        texts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
        if len(texts) > 1:
            return reply(info, "Klart, vi kan snakke om kaffe også.")
        if not returned(messages, "room_request"):
            return ModelResponse(parts=[ToolCallPart("room_request", {"action": "check_room", "room_name": "Fokus",
                "starts_at": start.isoformat(), "ends_at": (start + timedelta(minutes=30)).isoformat()})])
        return reply(info, "Fokus er ledig i demokalenderen; rommet er ikke reservert.")
    with conversation.assistant.override(model=FunctionModel(model)):
        first = say(client, "Er Fokus et ledig sted å samles i morgen på denne tiden?", event_id="room")
        assert first["room_workflow"]["state"] == "available"
        response = client.post(f"/api/requests/{first['id']}/transcript", json={"text": "Forresten, kan vi snakke om kaffe?"})
        assert response.status_code == 200, response.text
        assert response.json()["request"]["status"] == "resolved"
        assert "room_workflow" not in response.json()["request"]
    assert app.state.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_provider_failure_does_not_use_mock_or_erase_prepared_approval(live):
    client, app, _ = live
    def failure(messages, info):
        raise RuntimeError("provider unavailable")
    with conversation.assistant.override(model=FunctionModel(failure)):
        response = client.post("/api/input", json={"board_id": "kitchen", "text": "kaffe"}).json()
    assert response["status"] == "failed" and client.get("/api/state").json()["orders"] == []
    decision = {"intent": "purchase", "item_id": "cups", "summary": "Cups", "quantity": 1,
                "question": None, "assessment": assessment()}
    def partial(messages, info):
        if returned(messages, "submit_request"):
            raise RuntimeError("summary failed after tool")
        return ModelResponse(parts=[ToolCallPart("submit_request", {"decision": decision})])
    with conversation.assistant.override(model=FunctionModel(partial)):
        task = say(client, "skaff kopper")
    assert task["status"] == "awaiting_approval" and task["pending"]
    assert app.state.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


@pytest.mark.parametrize("room_first", [True, False])
def test_mixed_room_and_purchase_have_independent_approval_state(live, room_first):
    client, app, start = live
    decision = {"intent": "purchase", "item_id": "cups", "summary": "Cups", "quantity": 1,
                "question": None, "assessment": assessment()}
    steps = [("room_request", {"action": "check_room", "room_name": "Fokus", "starts_at": start.isoformat(),
                              "ends_at": (start + timedelta(minutes=30)).isoformat()}),
             ("submit_request", {"decision": decision})]
    if not room_first:
        steps.reverse()
    def model(messages, info):
        for name, args in steps:
            if not returned(messages, name):
                return ModelResponse(parts=[ToolCallPart(name, args)])
        return reply(info, "Fokus er ledig i demokalenderen, og koppeforslaget venter på godkjenning.")
    with conversation.assistant.override(model=FunctionModel(model)):
        say(client, "Sjekk Fokus i morgen, og lag et forslag på kopper også.")
    requests = client.get("/api/requests").json()
    quoted = [r for r in requests if r.get("proposal")]
    assert len(quoted) == 1 and quoted[0]["status"] == "awaiting_approval"
    task = db.row(app.state.conn, "SELECT * FROM tasks WHERE id=?", quoted[0]["id"])
    assert json.loads(task["pending"])[0]["details"]["proposal"] == quoted[0]["proposal"]
    assert client.post(f"/api/tasks/{task['id']}/decide", json={"approve": True}).status_code == 200
    assert app.state.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_new_room_interval_preserves_existing_queued_message(live):
    client, app, start = live
    def model(messages, info):
        # Each turn checks one interval. Earlier tool returns remain in history.
        last_prompt = max(i for i, m in enumerate(messages) if any(isinstance(p, UserPromptPart) for p in m.parts))
        if not returned(messages[last_prompt:], "room_request"):
            return ModelResponse(parts=[ToolCallPart("room_request", {"action": "check_room", "room_name": "Fokus",
                "starts_at": start.isoformat(), "ends_at": (start + timedelta(minutes=30)).isoformat()})])
        return reply(info, "Fokus er ledig i demokalenderen; ikke reservert.")
    with conversation.assistant.override(model=FunctionModel(model)):
        first = say(client, "Sjekk Fokus i morgen klokka 13.")
        value = app.state.rooms._load(first["id"])
        value.update(state="queued", message_id="persisted-message", message_body="Keep this association", reply="Message queued")
        app.state.rooms._update(first["id"], value, value["state"], value["reply"])
        second = say(client, "Sjekk Fokus til et annet møte i stedet.", conversation_id=first["id"], event_id="new-interval")
        assert second["id"] != first["id"]
        assert say(client, "Sjekk Fokus til et annet møte i stedet.", conversation_id=first["id"], event_id="new-interval")["id"] == second["id"]
    old = app.state.rooms._load(first["id"])
    assert old["message_id"] == "persisted-message" and old["state"] == "queued"


def test_provider_failure_preserves_queued_room_conversation(live):
    client, app, _ = live
    task_id = agent.start_task(app.state.conn, "kitchen", "unknown", "Existing room conversation")
    value = {"board_id": "kitchen", "requester_name": "a colleague", "state": "queued", "reply": "Message queued",
             "message_id": "persisted-message", "history": [], "trace": []}
    app.state.rooms._update(task_id, value, "queued", "Message queued")
    def failure(messages, info):
        raise RuntimeError("provider unavailable")
    with conversation.assistant.override(model=FunctionModel(failure)):
        result = say(client, "Har meldingen kommet fram?", conversation_id=task_id)
    assert result["status"] == "running" and result["room_workflow"]["state"] == "queued"
    assert result["room_workflow"]["message_id"] == "persisted-message"


def test_live_proposal_preserves_merged_work_and_whatsapp_confirmation(live, tmp_path, monkeypatch):
    """One live-interpreted quote uses the existing approval queue and outcome tracking."""
    _, prior_app, _ = live
    monkeypatch.setenv("OFFICE_ORDER_PUBLIC_URL", "https://demo.example")
    monkeypatch.setenv("OFFICE_ORDER_WHATSAPP_TO", "+12025550123")
    token_path = tmp_path / "confirmation-token"
    app = create_app(":memory:", room_calendar_path=prior_app.state.rooms.directory.path,
                     bridge_token_path=token_path)
    decision = {"intent": "purchase", "item_id": "cups", "summary": "Cups", "quantity": 2,
                "question": None, "assessment": assessment()}
    calls = []
    def model(messages, info):
        calls.append(1)
        if not returned(messages, "submit_request"):
            return ModelResponse(parts=[ToolCallPart("submit_request", {"decision": decision})])
        return reply(info, "To pakker kopper er klare for nøyaktig godkjenning; dette er simulert.")
    with TestClient(app) as client, conversation.assistant.override(model=FunctionModel(model)):
        task = say(client, "Skaff to pakker kopper", event_id="merged-live-order")
        assert task["status"] == "awaiting_approval"
        work = client.get("/api/work").json()["items"]
        linked = next(item for item in work if item.get("links", {}).get("task_id") == task["id"])
        assert linked["status"] == "awaiting_approval"
        queued = client.portal.call(app.state.order_confirmations.queue_request, task["id"])
        headers = {"Authorization": "Bearer " + token_path.read_text()}
        heartbeat = client.post("/api/whatsapp/heartbeat", headers=headers,
                                json={"consumer_id": "merged-bridge", "connected": True})
        assert heartbeat.status_code == 200
        jobs = client.post("/api/whatsapp/jobs/claim", headers=headers,
                           json={"consumer_id": "merged-bridge"}).json()["jobs"]
        assert len(jobs) == 1 and jobs[0]["message_id"] == queued["message_id"]
        assert "2 ×" in jobs[0]["body"]
        approval_token = jobs[0]["body"].split("/confirm/", 1)[1].strip()
        assert client.get("/confirm/" + approval_token).status_code == 200
        approved = client.post("/confirm/" + approval_token)
        assert approved.status_code == 200, approved.text
        assert len(calls) == 2  # approval never asks Claude to execute or reinterpret
        state = client.get("/api/state").json()
        assert len(state["orders"]) == 1 and state["orders"][0]["quantity"] == 2
        work = client.get("/api/work").json()["items"]
        linked = next(item for item in work if item.get("links", {}).get("task_id") == task["id"])
        assert linked["status"] == "waiting"
        assert client.get("/api/work/inventory").json()["items"] == []


def test_dashboard_chat_preserves_history_and_stage_sensor_tool(live):
    from office.demo_history import seed
    client, app, _ = live
    seed(app.state.conn)
    calls = []
    def model(messages, info):
        calls.append(messages)
        assert 'read_environment_history' in {t.name for t in info.function_tools}
        assert 'room_request' in {t.name for t in info.function_tools}
        if not returned(messages, 'read_environment_history'):
            return ModelResponse(parts=[ToolCallPart('read_environment_history', {'board_id': 'kitchen'})])
        samples = returned(messages, 'read_environment_history')[-1]
        assert len(samples) == 6 and all(r['simulation'] for r in samples)
        return reply(info, 'Two earlier simulated checks showed high CO2 at the stage; no booking made.')
    with conversation.assistant.override(model=FunctionModel(model)):
        body = {'conversation_id': 'stage-chat', 'event_id': 'stage-first', 'board_id': 'kitchen',
                'text': 'Could we meet at the stage?'}
        first = client.post('/api/chat', json=body)
        assert first.status_code == 200 and first.json()['request']['status'] == 'resolved', first.text
        second = client.post('/api/chat', json={**body, 'event_id': 'stage-followup', 'text': 'What about earlier conditions?'})
        assert second.status_code == 200 and second.json()['request']['status'] == 'resolved', second.text
        prompts = [p.content for m in calls[-1] for p in m.parts if isinstance(p, UserPromptPart)]
        assert body['text'] in prompts and 'What about earlier conditions?' in prompts
        count = len(calls)
        assert client.post('/api/chat', json=body).json()['duplicate']
        assert len(calls) == count
