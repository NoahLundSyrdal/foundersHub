"""Explicit synthetic room scenario, isolated from real telemetry and actions.

The CLI calls the local API and never opens a runtime database directly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from office import config
from office.facility_rules import FacilityReading


class _CurrentValues(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    temperature_c: float = Field(ge=-40, le=125)
    humidity_pct: float = Field(ge=0, le=100)
    co2_ppm: float = Field(ge=1, le=100000)


class _Fixture(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    simulation: Literal[True]
    label: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=128)
    history_days: Literal[28]
    interval_minutes: Literal[10]
    hot_start_hour: int = Field(ge=0, le=23)
    hot_end_hour: int = Field(ge=1, le=24)
    hot_temperature_c: float = Field(ge=-39, le=124)
    mild_temperature_c: float = Field(ge=-39, le=124)
    night_temperature_c: float = Field(ge=-39, le=124)
    humidity_pct: float = Field(ge=3, le=97)
    co2_ppm: float = Field(ge=101, le=99900)
    current_values: _CurrentValues

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError('Synthetic room timezone must be a valid IANA name') from error
        return value

    @model_validator(mode='after')
    def valid_day(self):
        if self.hot_end_hour <= self.hot_start_hour:
            raise ValueError('Synthetic hot hours must be an increasing daytime interval')
        return self


class _DemoInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    board_id: str = Field(min_length=1, max_length=64)
    action: Literal['seed', 'clear']


def _known_board(board_id):
    if not isinstance(board_id, str) or board_id not in config.BOARDS:
        raise ValueError('Synthetic room scenario requires a known configured board')


def _now(value):
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Synthetic seed timestamp must include a timezone')
    return value.astimezone(UTC)


def _schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS room_demo_datasets(id TEXT PRIMARY KEY,board_id TEXT UNIQUE,body TEXT)')
    conn.execute('''CREATE TABLE IF NOT EXISTS room_demo_samples(
        dataset_id TEXT,metric TEXT,value REAL,at TEXT,PRIMARY KEY(dataset_id,metric,at))''')


def active_dataset(conn, board_id):
    """Read active scenario metadata; absence never creates a table or mapping."""
    if board_id is None:
        return None
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='room_demo_datasets'").fetchone()
    if not exists:
        return None
    row = conn.execute('SELECT body FROM room_demo_datasets WHERE board_id=?', (board_id,)).fetchone()
    return json.loads(row[0]) if row else None


def seed(conn, board_id, now=None):
    _known_board(board_id)
    at = _now(now)
    fixture_path = Path(__file__).resolve().parents[3] / 'fixtures/room-environment-demo.json'
    fixture = _Fixture.model_validate(json.loads(fixture_path.read_text()))
    zone = ZoneInfo(fixture.timezone)
    today = at.astimezone(zone).date()
    dataset_id = 'room-demo-' + hashlib.sha256(f'{board_id}:{today.isoformat()}'.encode()).hexdigest()[:24]
    with _Savepoint(conn):
        _schema(conn)
        previous = active_dataset(conn, board_id)
        if previous and previous.get('local_date') == today.isoformat():
            return previous
        if previous:
            conn.execute('DELETE FROM room_demo_samples WHERE dataset_id=?', (previous['dataset_id'],))
            conn.execute('DELETE FROM room_demo_datasets WHERE board_id=?', (board_id,))
        samples = []
        first = today - timedelta(days=fixture.history_days)
        for offset in range(fixture.history_days):
            date = first + timedelta(days=offset)
            cursor = datetime.combine(date, time.min, zone).astimezone(UTC)
            end = datetime.combine(date + timedelta(days=1), time.min, zone).astimezone(UTC)
            # Calendar-day rotation is independent of board, insertion order and
            # retry timing. Four of each five prior dates are hot during the day.
            hot_day = date.toordinal() % 5 != 0
            while cursor < end:
                local = cursor.astimezone(zone)
                daytime = fixture.hot_start_hour <= local.hour < fixture.hot_end_hour
                base = fixture.hot_temperature_c if daytime and hot_day else fixture.mild_temperature_c if daytime else fixture.night_temperature_c
                variation = (((date.toordinal() + local.hour * 6 + local.minute // 10) % 9) - 4) * 0.05
                values = {'temperature_c': round(base + variation, 2),
                          'humidity_pct': round(fixture.humidity_pct + variation * 5, 2),
                          'co2_ppm': round(fixture.co2_ppm + variation * 250, 2)}
                timestamp = cursor.isoformat(timespec='seconds')
                for metric, value in values.items():
                    # Reuse strict canonical metric/value/time validation; these
                    # records remain exclusively in the synthetic sample table.
                    sample = FacilityReading(event_id=f'{dataset_id}:{metric}:{int(cursor.timestamp())}',
                        board_id=board_id, metric=metric, value=value, timestamp=timestamp, simulation=True)
                    samples.append((dataset_id, metric, sample.value, sample.timestamp))
                cursor += timedelta(minutes=fixture.interval_minutes)
        metadata = {'dataset_id': dataset_id, 'board_id': board_id, 'simulation': True, 'samples': len(samples),
                    'days': fixture.history_days, 'seeded_at': at.isoformat(timespec='milliseconds'),
                    'current_values': fixture.current_values.model_dump(), 'label': fixture.label,
                    'timezone': fixture.timezone, 'local_date': today.isoformat(),
                    'date_range': {'start': first.isoformat(), 'end': (today - timedelta(days=1)).isoformat()},
                    'interval_minutes': fixture.interval_minutes}
        conn.execute('INSERT INTO room_demo_datasets VALUES(?,?,?)',
                     (dataset_id, board_id, json.dumps(metadata, sort_keys=True, separators=(',', ':'), allow_nan=False)))
        conn.executemany('INSERT INTO room_demo_samples VALUES(?,?,?,?)', samples)
        return metadata


def clear(conn, board_id):
    _known_board(board_id)
    with _Savepoint(conn):
        previous = active_dataset(conn, board_id)
        if previous is None:
            return {'board_id': board_id, 'simulation': True, 'cleared': False, 'dataset_id': None, 'samples': 0}
        conn.execute('DELETE FROM room_demo_samples WHERE dataset_id=?', (previous['dataset_id'],))
        conn.execute('DELETE FROM room_demo_datasets WHERE board_id=?', (board_id,))
        return {'board_id': board_id, 'simulation': True, 'cleared': True,
                'dataset_id': previous['dataset_id'], 'samples': previous['samples']}


def router(conn, lock):
    routes = APIRouter()

    @routes.post('/api/demo/room-environment')
    async def demo(body: _DemoInput):
        async with lock:
            try:
                _known_board(body.board_id)
                return seed(conn, body.board_id) if body.action == 'seed' else clear(conn, body.board_id)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error

    return routes


class _Savepoint:
    def __init__(self, conn):
        self.conn, self.name = conn, 'room_demo_' + uuid4().hex

    def __enter__(self):
        self.conn.execute(f'SAVEPOINT {self.name}')

    def __exit__(self, error_type, error, traceback):
        if error_type:
            self.conn.execute(f'ROLLBACK TO {self.name}')
        self.conn.execute(f'RELEASE {self.name}')
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description='Seed or clear an explicitly synthetic room scenario through the local API.')
    parser.add_argument('--api', default='http://127.0.0.1:3001')
    parser.add_argument('--board', default='kitchen')
    parser.add_argument('--clear', action='store_true')
    args = parser.parse_args(argv)
    parsed = urlsplit(args.api)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('--api must be a plain HTTP loopback API address')
    payload = json.dumps({'board_id': args.board, 'action': 'clear' if args.clear else 'seed'}).encode()
    request = Request(args.api.rstrip('/') + '/api/demo/room-environment', data=payload,
                      headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=20) as response:
            print(json.dumps(json.load(response), indent=2))
    except (HTTPError, URLError) as error:
        parser.exit(1, f'Synthetic room API request failed: {error}\n')


if __name__ == '__main__':
    main()
