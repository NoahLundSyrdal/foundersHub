import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from office import config, db
from office.api import create_app
from office.demo_history import seed
from office.plugins.sensors import read_environment_history, read_sensors


def test_seed_is_small_idempotent_simulated_and_location_scoped():
    conn = db.connect(':memory:')
    seed(conn)
    seed(conn)
    assert conn.execute('SELECT count(*) FROM facility_events').fetchone()[0] == 6
    assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 2
    assert conn.execute('SELECT count(*) FROM facility_incidents').fetchone()[0] == 0
    ctx = SimpleNamespace(deps=SimpleNamespace(db=conn, board=config.BOARDS['kitchen']))
    history = asyncio.run(read_environment_history(ctx))
    assert len(history) == 6
    assert len({r['timestamp'] for r in history}) == 2
    assert all(r['simulation'] and r['historical'] for r in history)
    assert asyncio.run(read_sensors(ctx)) == []
    assert asyncio.run(read_environment_history(ctx, 'printer')) == []
    assert conn.execute('SELECT count(*) FROM orders').fetchone()[0] == 0


def test_old_history_is_not_used():
    conn = db.connect(':memory:')
    seed(conn, datetime.now(timezone.utc) - timedelta(days=8))
    ctx = SimpleNamespace(deps=SimpleNamespace(db=conn, board=config.BOARDS['kitchen']))
    assert asyncio.run(read_environment_history(ctx)) == []


def test_meeting_demo_uses_history_without_booking():
    app = create_app(':memory:')
    seed(app.state.conn)
    with TestClient(app) as client:
        history = client.get('/api/requests').json()
        assert len(history) == 2
        assert all(r['status'] == 'resolved' and r['intent'] == 'conversation' for r in history)
        response = client.post('/api/chat', json={'conversation_id': 'demo-test', 'event_id': 'meeting-test',
            'board_id': 'kitchen', 'text': 'Can you book a meeting here at the stage?'})
        assert response.status_code == 200, response.text
        tasks = db.rows(app.state.conn, "SELECT reply FROM tasks WHERE id NOT LIKE 'demo-stage-%'")
        assert len(tasks) == 1
        assert 'two earlier simulated checks' in tasks[0]['reply']
        assert 'no booking made' in tasks[0]['reply']
        assert app.state.conn.execute('SELECT count(*) FROM orders').fetchone()[0] == 0
