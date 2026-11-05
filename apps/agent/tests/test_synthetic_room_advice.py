"""Explicit synthetic fixtures power the room demo without contaminating sensors."""
from datetime import UTC, datetime, timedelta
import json
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from office import agent, config, procurement_workflow
from office.api import create_app
from office.room_conditions import RoomConditions
from office.room_service import RoomQuery

OSLO = ZoneInfo('Europe/Oslo')


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv('OFFICE_WHATSAPP_ENABLED', 'false')
    monkeypatch.setenv('ODA_FOOD_ENABLED', 'false')
    monkeypatch.setattr(config, 'MODEL', 'mock')
    async def forbidden(*args, **kwargs):
        raise AssertionError('Synthetic room answers must not call purchasing or the model')
    monkeypatch.setattr(agent.office_agent, 'run', forbidden)
    monkeypatch.setattr(procurement_workflow, 'prepare_need', forbidden)
    start = (datetime.now(OSLO) + timedelta(days=1)).replace(hour=13, minute=0, second=0, microsecond=0)
    async def parser(context):
        return RoomQuery(action='check_room', room_name='Demo room', starts_at=start,
                         ends_at=start + timedelta(minutes=30), reply='Checking the requested interval.')
    calendar = tmp_path / 'calendar.json'
    calendar.write_text(json.dumps({'rooms': [{'name': 'Demo room', 'board_id': 'kitchen', 'simulation': True}],
                                    'bookings': [], 'contacts': []}))
    application = create_app(':memory:', room_calendar_path=calendar, bridge_token_path=tmp_path / 'token', room_parser=parser)
    with TestClient(application) as client:
        yield client, application.state.conn, start


def seed(client):
    result = client.post('/api/demo/room-environment', json={'board_id': 'kitchen', 'action': 'seed'})
    assert result.status_code == 200, result.text
    return result.json()


def test_seed_enables_labelled_sensor_answers_and_hot_meeting_advice(demo):
    client, conn, _ = demo
    seeded = seed(client)
    assert seeded['simulation'] is True and seeded['days'] == 28 and seeded['samples'] > 10000
    result = client.post('/api/input', json={'board_id': 'kitchen', 'text': 'What is the temperature, humidity and CO2?'}).json()
    assert 'Synthetic demo' in result['reply'] and '29.2' in result['reply'] and '45' in result['reply']
    assert result['environment']['current']['simulation'] is True
    assert all(r['state'] == 'demo' for r in result['environment']['current']['readings'])
    view = next(r for r in client.get('/api/requests').json() if r['id'] == result['id'])
    assert view['simulation'] is True and view['status'] == 'resolved'
    meeting = client.post('/api/input', json={'board_id': 'kitchen', 'text': 'Book this meeting room tomorrow at 13:00 for an important meeting'}).json()
    advice = meeting['room_workflow']['environment']
    assert advice['recommendation'] == 'avoid'
    assert advice['history']['basis'] == 'synthetic' and advice['history']['state'] == 'usually_hot'
    assert advice['history']['dataset_id'] == seeded['dataset_id']
    assert 'Synthetic demo history' in meeting['reply'] and 'another room' in meeting['reply']
    assert 'not reserved' in meeting['reply']
    for table in ('sensor_readings', 'orders', 'tickets', 'whatsapp_delivery_jobs'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert client.get('/api/air-quality').json()['level'] == 'unknown'


def test_fresh_physical_values_and_qualified_physical_history_take_priority(demo):
    client, conn, start = demo
    seed(client)
    now = datetime.now(UTC)
    conn.execute('INSERT INTO sensor_readings VALUES(?,?,?,?)', ('kitchen', 'temperature_c', 22, now.isoformat()))
    checks = RoomConditions(conn)
    current = checks.snapshot('kitchen')
    assert current['basis'] == 'physical' and current['simulation'] is False
    assert current['readings'][0]['value'] == 22
    assert current['readings'][1]['value'] is None  # never blend live and synthetic
    days = 0
    for offset in range(1, 29):
        day = now.astimezone(OSLO).date() - timedelta(days=offset)
        if (day.weekday() < 5) != (start.weekday() < 5):
            continue
        at = datetime.combine(day, start.timetz())
        for minute in (0, 10, 20):
            conn.execute('INSERT INTO sensor_readings VALUES(?,?,?,?)',
                ('kitchen', 'temperature_c', 22, (at + timedelta(minutes=minute)).isoformat()))
        days += 1
        if days == 3:
            break
    advice = checks.assess('kitchen', start, start + timedelta(minutes=30))
    assert advice['history']['basis'] == 'physical' and advice['history']['simulation'] is False
    assert advice['history']['dataset_id'] is None and advice['history']['state'] == 'no_hot_pattern'
    assert 'Synthetic' not in advice['summary']
    before = conn.execute('SELECT COUNT(*) FROM sensor_readings').fetchone()[0]
    result = client.post('/api/demo/room-environment', json={'board_id': 'kitchen', 'action': 'clear'})
    assert result.status_code == 200, result.text
    assert conn.execute('SELECT COUNT(*) FROM sensor_readings').fetchone()[0] == before
    assert RoomConditions(conn).snapshot('kitchen')['readings'][0]['value'] == 22


def test_clear_disables_demo_and_does_not_affect_other_stations(demo):
    client, conn, start = demo
    seed(client)
    assert all(r['value'] is None for r in RoomConditions(conn).snapshot('printer')['readings'])
    assert RoomConditions(conn).snapshot(None)['basis'] == 'physical'
    assert seed(client) == seed(client)
    assert client.post('/api/demo/room-environment', json={'board_id': 'kitchen', 'action': 'clear'}).status_code == 200
    assert all(r['value'] is None for r in RoomConditions(conn).snapshot('kitchen')['readings'])
    assert RoomConditions(conn).assess('kitchen', start, start + timedelta(minutes=30))['history']['state'] == 'insufficient'
