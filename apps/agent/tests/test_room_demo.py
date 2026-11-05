import asyncio
from datetime import UTC, datetime, timedelta
import json
import sqlite3
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from office import db
from office import room_demo

OSLO = ZoneInfo('Europe/Oslo')
NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


@pytest.fixture
def conn():
    value = db.connect(':memory:')
    yield value
    value.close()


def test_absent_dataset_lookup_is_read_only_without_schema_creation():
    conn = sqlite3.connect(':memory:')
    try:
        before = list(conn.execute('SELECT name FROM sqlite_master'))
        assert room_demo.active_dataset(conn, 'kitchen') is None
        assert room_demo.active_dataset(conn, None) is None
        assert list(conn.execute('SELECT name FROM sqlite_master')) == before
        assert conn.total_changes == 0
    finally:
        conn.close()


def test_same_local_date_seed_is_idempotent_and_preserves_metadata(conn):
    first = room_demo.seed(conn, 'kitchen', NOW)
    assert first['simulation'] is True and first['days'] == 28
    assert first['current_values'] == {'temperature_c': 29.2, 'humidity_pct': 45.0, 'co2_ppm': 1300.0}
    rows = list(conn.execute('SELECT * FROM room_demo_samples ORDER BY at,metric'))
    changes = conn.total_changes
    second = room_demo.seed(conn, 'kitchen', NOW + timedelta(hours=3))
    assert second == first
    assert conn.total_changes == changes
    assert [tuple(row) for row in conn.execute('SELECT * FROM room_demo_samples ORDER BY at,metric')] == [tuple(row) for row in rows]
    assert room_demo.active_dataset(conn, 'kitchen') == first


def test_next_local_day_replaces_only_that_boards_data(conn):
    old = room_demo.seed(conn, 'kitchen', NOW)
    other = room_demo.seed(conn, 'bathroom', NOW)
    next_day = room_demo.seed(conn, 'kitchen', NOW + timedelta(days=1))
    assert next_day['dataset_id'] != old['dataset_id']
    assert conn.execute('SELECT COUNT(*) FROM room_demo_samples WHERE dataset_id=?', (old['dataset_id'],)).fetchone()[0] == 0
    assert room_demo.active_dataset(conn, 'bathroom') == other
    assert conn.execute('SELECT COUNT(*) FROM room_demo_samples WHERE dataset_id=?', (other['dataset_id'],)).fetchone()[0] == other['samples']
    assert conn.execute('SELECT COUNT(*) FROM room_demo_datasets').fetchone()[0] == 2


def test_clear_is_idempotent_and_board_scoped_without_creating_tables(conn):
    assert room_demo.clear(conn, 'kitchen')['cleared'] is False
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='room_demo_samples'").fetchone()
    first = room_demo.seed(conn, 'kitchen', NOW)
    other = room_demo.seed(conn, 'bathroom', NOW)
    result = room_demo.clear(conn, 'kitchen')
    assert result['cleared'] is True and result['dataset_id'] == first['dataset_id']
    assert room_demo.active_dataset(conn, 'kitchen') is None
    assert room_demo.active_dataset(conn, 'bathroom') == other
    assert room_demo.clear(conn, 'kitchen')['cleared'] is False
    assert conn.execute('SELECT COUNT(*) FROM room_demo_samples').fetchone()[0] == other['samples']


def test_history_excludes_today_covers_28_calendar_days_and_dst(conn):
    metadata = room_demo.seed(conn, 'kitchen', NOW)
    rows = conn.execute('SELECT metric,value,at FROM room_demo_samples WHERE dataset_id=?', (metadata['dataset_id'],)).fetchall()
    assert len(rows) == metadata['samples']
    local_dates = set()
    temperature_counts = {}
    for metric, value, stamp in rows:
        at = datetime.fromisoformat(stamp)
        assert at.tzinfo is not None
        local = at.astimezone(OSLO)
        local_dates.add(local.date())
        bounds = {'temperature_c': (-40, 125), 'humidity_pct': (0, 100), 'co2_ppm': (1, 100000)}[metric]
        assert bounds[0] <= value <= bounds[1]
        if metric == 'temperature_c':
            temperature_counts[local.date().isoformat()] = temperature_counts.get(local.date().isoformat(), 0) + 1
    assert len(local_dates) == 28
    assert min(local_dates).isoformat() == '2026-10-08'
    assert max(local_dates).isoformat() == '2026-11-04'
    assert NOW.astimezone(OSLO).date() not in local_dates
    assert temperature_counts['2026-10-25'] == 150  # 25-hour autumn day
    assert temperature_counts['2026-10-24'] == 144
    assert metadata['date_range'] == {'start': '2026-10-08', 'end': '2026-11-04'}


def test_spring_dst_day_has_no_invented_local_hour(conn):
    metadata = room_demo.seed(conn, 'kitchen', datetime(2026, 4, 1, 12, tzinfo=UTC))
    rows = conn.execute("SELECT at FROM room_demo_samples WHERE dataset_id=? AND metric='temperature_c'", (metadata['dataset_id'],))
    local = [datetime.fromisoformat(row[0]).astimezone(OSLO) for row in rows]
    spring = [at for at in local if at.date().isoformat() == '2026-03-29']
    assert len(spring) == 138  # 23-hour spring day
    assert all(at.hour != 2 for at in spring)


def test_hot_business_time_majority_exists_in_weekday_and_weekend_classes(conn):
    metadata = room_demo.seed(conn, 'kitchen', NOW)
    rows = conn.execute("SELECT value,at FROM room_demo_samples WHERE dataset_id=? AND metric='temperature_c'", (metadata['dataset_id'],))
    grouped = {}
    for value, at in rows:
        local = datetime.fromisoformat(at).astimezone(OSLO)
        if local.hour == 13:
            grouped.setdefault(local.date(), []).append(value)
        if local.hour == 2:
            assert 21.5 <= value <= 22.5
    for weekday in (True, False):
        means = [sum(values) / len(values) for date, values in grouped.items() if (date.weekday() < 5) == weekday]
        assert len(means) >= 3
        assert sum(value > 27 for value in means) / len(means) >= 0.6


def test_seed_and_clear_do_not_touch_physical_data_or_workflows(conn):
    conn.execute('INSERT INTO sensor_readings VALUES(?,?,?,?)', ('kitchen', 'temperature_c', 19, NOW.isoformat()))
    before = [tuple(row) for row in conn.execute('SELECT * FROM sensor_readings')]
    room_demo.seed(conn, 'kitchen', NOW)
    assert [tuple(row) for row in conn.execute('SELECT * FROM sensor_readings')] == before
    for table in ('tasks', 'orders', 'tickets', 'board_commands', 'device_events'):
        assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    room_demo.clear(conn, 'kitchen')
    assert [tuple(row) for row in conn.execute('SELECT * FROM sensor_readings')] == before


def test_unknown_board_and_naive_seed_time_are_rejected(conn):
    for function in (room_demo.seed, room_demo.clear):
        with pytest.raises(ValueError, match='known configured board'):
            function(conn, 'unknown')
    with pytest.raises(ValueError, match='timezone'):
        room_demo.seed(conn, 'kitchen', NOW.replace(tzinfo=None))
    assert room_demo.active_dataset(conn, 'unknown') is None


def test_seed_participates_in_callers_transaction(conn):
    conn.execute('BEGIN')
    room_demo.seed(conn, 'kitchen', NOW)
    conn.execute('ROLLBACK')
    assert room_demo.active_dataset(conn, 'kitchen') is None


def test_async_router_validates_and_serializes_mutations(conn):
    app = FastAPI()
    lock = asyncio.Lock()
    app.include_router(room_demo.router(conn, lock))
    with TestClient(app) as client:
        response = client.post('/api/demo/room-environment', json={'board_id': 'kitchen', 'action': 'seed'})
        assert response.status_code == 200, response.text
        assert response.json()['simulation'] is True
        assert client.post('/api/demo/room-environment', json={'board_id': 'kitchen', 'action': 'seed'}).json() == response.json()
        assert client.post('/api/demo/room-environment', json={'board_id': 'unknown', 'action': 'seed'}).status_code == 400
        assert client.post('/api/demo/room-environment', json={'board_id': 'kitchen', 'action': 'delete'}).status_code == 422
        assert client.post('/api/demo/room-environment', json={'board_id': 'kitchen', 'action': 'clear', 'extra': True}).status_code == 422
        assert client.post('/api/demo/room-environment', json={'board_id': 'kitchen', 'action': 'clear'}).json()['cleared'] is True


def test_cli_posts_only_to_local_api_without_opening_database(monkeypatch, capsys):
    seen = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{"simulation":true}'
    def send(request, timeout):
        seen.append((request.full_url, json.loads(request.data), request.get_method(), timeout))
        return Response()
    monkeypatch.setattr(room_demo, 'urlopen', send)
    with patch('sqlite3.connect', side_effect=AssertionError('CLI must not open a database')):
        room_demo.main(['--api', 'http://127.0.0.1:3001', '--board', 'kitchen'])
        room_demo.main(['--api', 'http://127.0.0.1:3001', '--board', 'bathroom', '--clear'])
    assert seen[0][:3] == ('http://127.0.0.1:3001/api/demo/room-environment', {'board_id': 'kitchen', 'action': 'seed'}, 'POST')
    assert seen[1][1] == {'board_id': 'bathroom', 'action': 'clear'}
    assert 'true' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        room_demo.main(['--api', 'https://example.com'])
