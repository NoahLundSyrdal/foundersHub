"""Named-room advice uses the same booking conversation and station input."""
from datetime import UTC, datetime, timedelta
import json
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from office import config
from office.api import create_app
from office.room_service import RoomQuery


@pytest.fixture
def room(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_WHATSAPP_ENABLED", "false")
    monkeypatch.setenv("ODA_FOOD_ENABLED", "false")
    monkeypatch.setattr(config, "MODEL", "mock")
    start = (datetime.now(ZoneInfo("Europe/Oslo")) + timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0)
    calendar = {"rooms": [{"name": "Test room", "simulation": True, "board_id": "kitchen"}], "bookings": [], "contacts": []}
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(calendar))
    calls = []
    async def parser(context):
        calls.append(context)
        return RoomQuery(action="check_room", room_name="Test room", starts_at=start,
                         ends_at=start + timedelta(minutes=30), reply="Checking the meeting interval.")
    app = create_app(":memory:", room_calendar_path=path, bridge_token_path=tmp_path / "token", room_parser=parser)
    with TestClient(app) as client:
        yield client, app, start, path, calendar, calls


def values(app, board="kitchen", temp=29):
    now = datetime.now(UTC).isoformat()
    for metric, value in (("temperature_c", temp), ("humidity_pct", 45), ("co2_ppm", 700)):
        app.state.conn.execute("INSERT INTO sensor_readings VALUES(?,?,?,?)", (board, metric, value, now))


def history(app, start, board="kitchen"):
    # Match requested weekday/weekend class and local time, each past day equal.
    today = datetime.now(ZoneInfo("Europe/Oslo")).date()
    days = 0
    for offset in range(1, 28):
        day = today - timedelta(days=offset)
        if (day.weekday() < 5) != (start.weekday() < 5):
            continue
        stamp = datetime.combine(day, start.timetz())
        for minute in (0, 10, 20):
            app.state.conn.execute("INSERT INTO sensor_readings VALUES(?,?,?,?)",
                (board, "temperature_c", 29, (stamp + timedelta(minutes=minute)).isoformat()))
        days += 1
        if days == 3:
            return


def test_booking_advice_has_actual_temperature_and_recorded_pattern(room):
    client, app, start, _, _, calls = room
    values(app)
    history(app, start)
    body = {"board_id": "kitchen", "text": "Book this meeting room for an important meeting tomorrow at 13:00", "event_id": "room-advice"}
    response = client.post("/api/input", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    environment = result["room_workflow"]["environment"]
    assert environment["recommendation"] == "avoid" and environment["history"]["state"] == "usually_hot"
    assert environment["history"]["days"] == environment["history"]["hot_days"] == 3
    assert "29" in result["reply"] and "3 of 3" in result["reply"] and "another room" in result["reply"]
    assert "not reserved" in result["reply"] and "Simulated calendar" in result["reply"]
    assert result["command"]["color"] == "amber"
    stages = [s["stage"] for s in result["trace"]]
    assert stages == ["checking", "checking_conditions", "checking_history", "available"]
    assert calls[0]["current_board_id"] == "kitchen"
    assert client.post("/api/input", json=body).json()["id"] == result["id"]
    assert len(calls) == 1
    assert app.state.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert app.state.conn.execute("SELECT COUNT(*) FROM whatsapp_delivery_jobs").fetchone()[0] == 0


def test_unmapped_room_does_not_borrow_another_boards_hot_sensor(room):
    client, app, _, path, calendar, _ = room
    values(app)
    calendar["rooms"][0].pop("board_id")
    path.write_text(json.dumps(calendar))
    result = client.post("/api/input", json={"board_id": "kitchen", "text": "Book meeting room Test room tomorrow"}).json()
    environment = result["room_workflow"]["environment"]
    assert environment["recommendation"] == "unknown"
    assert environment["current"]["board_id"] is None
    assert all(r["value"] is None for r in environment["current"]["readings"])
    assert "29" not in result["reply"]


def test_named_room_sensor_question_uses_its_mapping_not_requesting_button(room):
    client, app, _, _, _, calls = room
    values(app)
    values(app, board="printer", temp=21)
    result = client.post("/api/input", json={"board_id": "printer", "text": "What is the temperature in Test room?"}).json()
    assert "29" in result["reply"] and "21" not in result["reply"]
    assert result["room_workflow"]["environment"]["current"]["board_id"] == "kitchen"
    assert calls == []


def test_booking_has_no_usual_heat_claim_without_enough_days(room):
    client, app, _, _, _, _ = room
    values(app)
    result = client.post("/api/input", json={"board_id": "kitchen", "text": "Book this meeting room tomorrow"}).json()
    environment = result["room_workflow"]["environment"]
    assert environment["recommendation"] == "caution" and environment["history"]["state"] == "insufficient"
    assert "29" in result["reply"]
    assert "qualified prior days were hot" not in result["reply"]


def test_existing_single_button_task_can_check_named_room_conditions(room):
    client, app, _, _, _, _ = room
    values(app)
    button = client.post("/api/device-events", json={"event_id": "one-button", "station_id": "kitchen",
        "type": "button_pressed", "timestamp": datetime.now(UTC).isoformat(), "payload": {}}).json()
    task_id = button["request"]["id"]
    response = client.post(f"/api/requests/{task_id}/transcript", json={"text": "What is the temperature in Test room?", "event_id": "one-button-sensors"})
    assert response.status_code == 200, response.text
    assert response.json()["request"]["id"] == task_id
    assert response.json()["request"]["status"] == "resolved"
    assert "29" in response.json()["request"]["summary"]
