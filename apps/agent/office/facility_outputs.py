"""Durable station output commands. Delivery belongs to a separately verified consumer.

A claim is persisted before a command is returned. Dispatched commands are never
replayed automatically: a lost acknowledgement is an uncertain delivery outcome.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime


def _now():
    return datetime.now(UTC).isoformat(timespec='milliseconds')


def _identifier(value, label):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError(f'{label} must be a nonblank string of at most 128 characters without control characters')
    return value


def _detail(value):
    if not isinstance(value, str) or len(value) > 500 or '\x00' in value:
        raise ValueError('Detail must be a string of at most 500 characters without null characters')
    return value


class StationActions:
    """SQLite queue for bounded, at-most-once automatic station sound dispatch."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.execute('''CREATE TABLE IF NOT EXISTS facility_outputs (
            id TEXT PRIMARY KEY, incident_id TEXT NOT NULL UNIQUE, board_id TEXT NOT NULL,
            state TEXT NOT NULL, body TEXT NOT NULL
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS facility_outputs_pending ON facility_outputs(board_id,state)')

    def _get(self, id):
        row = self.conn.execute('SELECT body FROM facility_outputs WHERE id=?', (id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _store(self, command):
        self.conn.execute('''INSERT INTO facility_outputs VALUES(?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET state=excluded.state, body=excluded.body''',
                          (command['id'], command['incident_id'], command['board_id'], command['state'],
                           json.dumps(command, sort_keys=True, separators=(',', ':'), allow_nan=False)))

    def enqueue(self, incident: dict) -> dict:
        if not isinstance(incident, dict):
            raise ValueError('Incident must be an object')
        incident_id = _identifier(incident.get('id'), 'Incident id')
        board_id = _identifier(incident.get('board_id'), 'Board id')
        if type(incident.get('simulation')) is not bool:
            raise ValueError('Incident simulation must be a boolean')
        actions = incident.get('actions')
        if incident.get('status') != 'active' or not isinstance(actions, list) or 'sound' not in actions:
            raise ValueError('Only active incidents requesting sound can enqueue a station command')
        title = incident.get('title')
        if not isinstance(title, str) or not title.strip() or len(title) > 120 or '\x00' in title:
            raise ValueError('Incident title must be nonblank and at most 120 characters')
        id = 'sound-' + hashlib.sha256(incident_id.encode()).hexdigest()[:24]
        with _Savepoint(self.conn):
            existing = self._get(id)
            if existing:
                if (existing['incident_id'], existing['board_id'], existing['simulation'], existing['text']) != (
                        incident_id, board_id, incident['simulation'], title):
                    raise ValueError('Incident id is already bound to a different sound command')
                return existing
            timestamp = _now()
            command = {'id': id, 'incident_id': incident_id, 'board_id': board_id, 'kind': 'sound',
                       'pattern': 'attention', 'text': title, 'simulation': incident['simulation'],
                       'state': 'pending', 'consumer_id': None, 'created_at': timestamp,
                       'updated_at': timestamp, 'detail': ''}
            self._store(command)
            return command

    def snapshot(self, board_id: str) -> list[dict]:
        board_id = _identifier(board_id, 'Board id')
        rows = self.conn.execute('''SELECT body FROM facility_outputs WHERE board_id=?
            ORDER BY rowid DESC LIMIT 100''', (board_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def claim(self, board_id: str, consumer_id: str) -> list[dict]:
        board_id = _identifier(board_id, 'Board id')
        consumer_id = _identifier(consumer_id, 'Consumer id')
        with _Savepoint(self.conn):
            # Acquire a writer before reading pending rows; independent SQLite
            # consumers cannot read and dispatch the same pending command.
            self.conn.execute('UPDATE facility_outputs SET state=state WHERE 0')
            rows = self.conn.execute('''SELECT body FROM facility_outputs WHERE board_id=? AND state='pending'
                ORDER BY rowid LIMIT 100''', (board_id,)).fetchall()
            claimed = []
            for row in rows:
                command = json.loads(row[0])
                command.update(state='dispatched', consumer_id=consumer_id, updated_at=_now())
                self._store(command)
                claimed.append(command)
            return claimed

    def acknowledge(self, board_id: str, id: str, status: str, detail: str = '') -> dict:
        board_id = _identifier(board_id, 'Board id')
        id = _identifier(id, 'Command id')
        detail = _detail(detail)
        if status not in ('delivered', 'failed'):
            raise ValueError('Acknowledgement must be delivered or failed')
        with _Savepoint(self.conn):
            self.conn.execute('UPDATE facility_outputs SET state=state WHERE 0')
            command = self._get(id)
            if command is None:
                raise KeyError('Unknown station command')
            if command['board_id'] != board_id:
                raise ValueError('Station command belongs to another board')
            if command['state'] in ('delivered', 'failed'):
                if command['state'] != status or command['detail'] != detail:
                    raise ValueError('Terminal station acknowledgement cannot be changed')
                return command
            if command['state'] != 'dispatched':
                raise ValueError('Only a previously dispatched station command can be acknowledged')
            command.update(state=status, detail=detail, updated_at=_now())
            self._store(command)
            return command

    def cancel_for_incident(self, id: str) -> None:
        id = _identifier(id, 'Incident id')
        with _Savepoint(self.conn):
            self.conn.execute('UPDATE facility_outputs SET state=state WHERE 0')
            row = self.conn.execute('SELECT body FROM facility_outputs WHERE incident_id=?', (id,)).fetchone()
            if row:
                command = json.loads(row[0])
                if command['state'] == 'pending':
                    command.update(state='cancelled', detail='Incident recovered before dispatch', updated_at=_now())
                    self._store(command)


class _Savepoint:
    def __init__(self, conn):
        self.conn, self.name = conn, 'station_' + uuid.uuid4().hex

    def __enter__(self):
        self.conn.execute(f'SAVEPOINT {self.name}')

    def __exit__(self, error_type, error, traceback):
        if error_type:
            self.conn.execute(f'ROLLBACK TO {self.name}')
        self.conn.execute(f'RELEASE {self.name}')
        return False
