"""End-to-end through the HTTP API with the offline mock model (OFFICE_MODEL=mock is the default)."""

import pytest
from fastapi.testclient import TestClient

from office import config, plugins
from office.api import create_app

pytestmark = pytest.mark.skipif(config.MODEL != "mock", reason="tests use the offline mock model")


@pytest.fixture
def api():
    return TestClient(create_app(":memory:"))


def say(api, text, person=None, board="kitchen"):
    r = api.post("/api/input", json={"board_id": board, "text": text, "person": person})
    assert r.status_code == 200, r.text
    return r.json()


def orders(api):
    return api.get("/api/state").json()["orders"]


def test_within_limit_orders_immediately(api, monkeypatch):
    monkeypatch.setitem(config.OFFICE, "require_approval", False)
    task = say(api, "We need coffee", person="Leo")
    assert task["status"] == "done" and "SIMULATED" in task["reply"]
    assert [o["product_id"] for o in orders(api)] == ["coffee"]


def test_over_limit_needs_approval_and_approval_is_single_use(api):
    task = say(api, "we need toner", board="printer")
    assert task["status"] == "awaiting_approval" and orders(api) == []
    assert task["pending"][0]["args"] == {"product_id": "toner", "quantity": 1}
    assert task["pending"][0]["details"]["total_nok"] == task["proposal"]["total_nok"]

    done = api.post(f"/api/tasks/{task['id']}/decide", json={"approve": True}).json()
    assert done["status"] == "done" and len(orders(api)) == 1
    assert api.post(f"/api/tasks/{task['id']}/decide", json={"approve": True}).status_code == 409
    assert len(orders(api)) == 1


def test_denied_approval_orders_nothing(api):
    task = say(api, "we need toner", board="printer")
    done = api.post(f"/api/tasks/{task['id']}/decide", json={"approve": False}).json()
    assert done["status"] == "done" and orders(api) == []


def test_over_office_cap_always_needs_the_routed_human_even_for_manager(api):
    task = say(api, "we need 10 toner", person="Leo", board="printer")  # far above the 1000 NOK cap
    details = task["pending"][0]["details"]
    assert task["status"] == "awaiting_approval" and orders(api) == []
    assert details["approval_type"] == "purchase_over_cap" and details["approver"] == "Leo"


def test_no_duplicate_order_while_one_is_open(api):
    first = say(api, "We need coffee", person="Leo")
    assert api.post(f"/api/tasks/{first['id']}/decide", json={"approve": True}).status_code == 200
    task = say(api, "We need coffee", person="Leo")
    assert "Not ordering again" in task["reply"] and len(orders(api)) == 1


def test_device_event_retry_creates_one_task(api):
    ev = {"event_id": "e1", "station_id": "kitchen", "type": "speech_transcript", "timestamp": "2026-11-05T14:00:00Z", "payload": {"text": "we need cups", "person": "Leo"}}
    first, second = api.post("/api/device-events", json=ev).json(), api.post("/api/device-events", json=ev).json()
    assert not first["duplicate"] and second["duplicate"] and first["request"] == second["request"]
    assert len(api.get("/api/state").json()["tasks"]) == 1
    assert api.get("/api/devices/kitchen/command").json()["command"]["color"] == "amber"


def test_sensor_fires_once_on_threshold_crossing(api):
    assert api.post("/api/sensor", json={"board_id": "bathroom", "kind": "stock_level", "value": 50}).json() == {"stored": True}
    task = api.post("/api/sensor", json={"board_id": "bathroom", "kind": "stock_level", "value": 12}).json()
    assert task["status"] == "awaiting_approval"  # sensor is not a person: always asks
    assert api.post("/api/sensor", json={"board_id": "bathroom", "kind": "stock_level", "value": 10}).json() == {"stored": True}


def test_plugins_follow_board_modules():
    kitchen = plugins.for_board(config.BOARDS["kitchen"])  # has temperature_humidity
    printer = plugins.for_board(config.BOARDS["printer"])
    assert plugins.sensors.toolset in kitchen and plugins.sensors.toolset not in printer


def test_stock_low_device_event_proposes_one_unit(api):
    ev = {"event_id": "s1", "station_id": "bathroom", "type": "stock_low", "timestamp": "2026-11-05T14:00:00Z", "payload": {"item_id": "toilet-paper", "level": 15}}
    task = api.post("/api/device-events", json=ev).json()
    pending = api.get("/api/state").json()["tasks"][0]["pending"]
    assert task["command"]["color"] == "amber" and pending[0]["args"] == {"product_id": "toilet-paper", "quantity": 1}


def test_next_speaker_never_continues_previous_speakers_task(api):
    leo = say(api, "we need cups", person="Leo")
    guest = say(api, "we need coffee")  # right after, same board: must not inherit Leo's task or identity
    assert guest["id"] != leo["id"] and guest["person"] != "Leo"


def test_parallel_orders_in_one_turn_buy_once(api):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from office.agent import office_agent

    def three_calls(messages, info):
        if any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("place_order", {"product_id": "toner"}, tool_call_id=f"c{i}") for i in range(3)])

    with office_agent.override(model=FunctionModel(three_calls)):
        task = say(api, "toner please", board="printer")
        assert task["status"] == "awaiting_approval" and len(task["pending"]) == 3
        assert api.post(f"/api/tasks/{task['id']}/decide", json={"approve": True}).status_code == 200
    assert len(orders(api)) == 1


def test_audio_must_be_valid_base64(api):
    r = api.post("/api/input", json={"board_id": "kitchen", "audio_b64": "not base64!!"})
    assert r.status_code == 400
