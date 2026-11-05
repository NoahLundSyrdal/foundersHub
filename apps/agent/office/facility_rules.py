"""Persistent deterministic facility checks. No model calls or external side effects."""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Metric = Literal['co2_ppm', 'temperature_c', 'humidity_pct', 'stock_level', 'bin_fill_pct',
                 'obstruction_mm', 'noise_level', 'tilt_deg', 'heartbeat']
Action = Literal['alert', 'ticket', 'replenish', 'sound']
BOUNDS = {
    'co2_ppm': (1, 100000), 'temperature_c': (-40, 125), 'humidity_pct': (0, 100),
    'stock_level': (0, 100), 'bin_fill_pct': (0, 100), 'obstruction_mm': (0, 4000),
    'noise_level': (0, 32767), 'tilt_deg': (0, 180), 'heartbeat': (0, 1),
}


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return result.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec='milliseconds')


class FacilityReading(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    event_id: str = Field(min_length=1, max_length=128)
    board_id: str = Field(min_length=1, max_length=64)
    metric: Metric
    value: float
    timestamp: str
    simulation: bool

    @field_validator('event_id', 'board_id')
    @classmethod
    def bounded_identifier(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError('Identifiers must be nonblank without surrounding whitespace')
        return value

    @field_validator('timestamp')
    @classmethod
    def timestamp_with_zone(cls, value):
        return _iso(_time(value))

    @model_validator(mode='after')
    def plausible_value(self):
        low, high = BOUNDS[self.metric]
        if not low <= self.value <= high:
            raise ValueError(f'{self.metric} must be between {low} and {high}')
        return self


class FacilityRule(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    board_id: str = Field(min_length=1, max_length=64)
    metric: Metric
    enabled: bool
    direction: Literal['above', 'below', 'stale']
    trigger_value: float
    reset_value: float
    min_samples: int = Field(ge=1, le=10000)
    min_duration_seconds: float = Field(ge=0, le=86400)
    max_gap_seconds: float = Field(gt=0, le=3600)
    actions: list[Action] = Field(min_length=1, max_length=4)
    severity: Literal['routine', 'urgent', 'critical']
    item_id: str | None = Field(default=None, min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=500)

    @field_validator('id', 'board_id', 'title', 'summary')
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or value != value.strip():
            raise ValueError('Values must be nonblank without surrounding whitespace')
        return value

    @model_validator(mode='after')
    def coherent_rule(self):
        if len(set(self.actions)) != len(self.actions):
            raise ValueError('Actions must be unique')
        if self.direction == 'stale':
            if self.metric != 'heartbeat' or not 1 <= self.trigger_value <= 86400:
                raise ValueError('Stale rules require heartbeat and a timeout of 1–86400 seconds')
            if any(x in self.actions for x in ('replenish', 'ticket', 'sound')):
                raise ValueError('Stale telemetry may only raise an alert')
        else:
            if self.metric == 'heartbeat':
                raise ValueError('Heartbeat requires stale direction')
            low, high = BOUNDS[self.metric]
            if not (low <= self.trigger_value <= high and low <= self.reset_value <= high):
                raise ValueError('Rule thresholds exceed the metric measurement range')
            if self.direction == 'above' and self.reset_value >= self.trigger_value:
                raise ValueError('Above rules must reset below their trigger')
            if self.direction == 'below' and self.reset_value <= self.trigger_value:
                raise ValueError('Below rules must reset above their trigger')
        if 'replenish' in self.actions and (self.metric != 'stock_level' or not self.item_id):
            raise ValueError('Replenishment requires calibrated stock_level and an item_id')
        if self.metric != 'stock_level' and self.item_id is not None:
            raise ValueError('Only stock rules can specify item_id')
        if self.metric == 'noise_level' and 'sound' in self.actions:
            raise ValueError('Noise checks cannot emit sound into their own sensor')
        return self


class FacilityEngine:
    """Call under the application's mutation lock; all state belongs to its SQLite store."""

    def __init__(self, conn: sqlite3.Connection, rules_path: str | Path | None = None,
                 clock: Callable[[], datetime | float] | None = None):
        self.conn = conn
        self.clock = clock or (lambda: datetime.now(UTC))
        self.rules_path = Path(rules_path) if rules_path else Path(__file__).resolve().parents[3] / 'config/facility-rules.json'
        for statement in (
            'CREATE TABLE IF NOT EXISTS facility_rules (id TEXT PRIMARY KEY, body TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS facility_readings (stream TEXT PRIMARY KEY, body TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS facility_events (id TEXT PRIMARY KEY, body TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS facility_states (id TEXT PRIMARY KEY, body TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS facility_incidents (id TEXT PRIMARY KEY, body TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS facility_heartbeats (id TEXT PRIMARY KEY, body TEXT NOT NULL)',
        ):
            conn.execute(statement)
        self.defaults = {rule.id: rule for rule in (FacilityRule.model_validate(x) for x in json.loads(self.rules_path.read_text()))}
        if len(self.defaults) != len(json.loads(self.rules_path.read_text())):
            raise ValueError('Duplicate facility rule ids')
        for rule in self.defaults.values():
            if self._get('facility_rules', rule.id) is None:
                self._put('facility_rules', rule.id, rule.model_dump())
            if rule.direction == 'stale' and self._get('facility_heartbeats', self._heartbeat_key(rule.board_id, False)) is None:
                self._put('facility_heartbeats', self._heartbeat_key(rule.board_id, False),
                          {'board_id': rule.board_id, 'simulation': False, 'timestamp': _iso(self._now())})

    def _now(self, now=None):
        value = self.clock() if now is None else now
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        if isinstance(value, str):
            return _time(value)
        if value.tzinfo is None:
            raise ValueError('Clock must return a timezone-aware datetime')
        return value.astimezone(UTC)

    def _get(self, table, key):
        row = self.conn.execute(f'SELECT body FROM {table} WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, table, key, body):
        self.conn.execute(f'INSERT INTO {table} VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                          (key, json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False)))

    def _all(self, table):
        return [json.loads(row[0]) for row in self.conn.execute(f'SELECT body FROM {table}')]

    @staticmethod
    def _heartbeat_key(board, simulation):
        return f'{board}:{int(simulation)}'

    @staticmethod
    def _state_key(rule, simulation):
        return f'{rule.id}:{int(simulation)}'

    def _rules(self):
        return [FacilityRule.model_validate(x) for x in self._all('facility_rules')]

    def snapshot(self, now=None):
        return {'rules': [r.model_dump() for r in self._rules()],
                'incidents': sorted(self._all('facility_incidents'), key=lambda i: i['opened_at'], reverse=True),
                'readings': [json.loads(row[0]) for row in self.conn.execute('SELECT body FROM facility_readings')],
                'evaluated_at': _iso(self._now(now))}

    def save_rule(self, rule: FacilityRule):
        rule = FacilityRule.model_validate(rule.model_dump() if isinstance(rule, FacilityRule) else rule)
        original = self.defaults.get(rule.id)
        if original is None:
            raise KeyError('Unknown facility rule')
        if (rule.board_id, rule.metric, rule.direction, rule.item_id) != (original.board_id, original.metric, original.direction, original.item_id):
            raise ValueError('Rule identity, board, metric, direction and configured item are immutable')
        existing = self._get('facility_rules', rule.id)
        if existing == rule.model_dump():
            return existing
        with self._transaction():
            self._put('facility_rules', rule.id, rule.model_dump())
            for sim in (False, True):
                key = self._state_key(rule, sim)
                state = self._get('facility_states', key)
                # A configuration edit is not a measured recovery. Keep an active
                # episode and its original action snapshot latched, even when disabled.
                # Only pending debounce samples belong to the superseded settings.
                if not state or not state.get('incident_id'):
                    self.conn.execute('DELETE FROM facility_states WHERE id=?', (key,))
        return rule.model_dump()

    def _transaction(self):
        # SAVEPOINT composes safely with an outer transaction as well as autocommit.
        return _Savepoint(self.conn)

    def ingest(self, reading: FacilityReading):
        reading = FacilityReading.model_validate(reading.model_dump() if isinstance(reading, FacilityReading) else reading)
        payload = reading.model_dump()
        seen = self._get('facility_events', reading.event_id)
        if seen is not None:
            if seen != payload:
                raise ValueError('Event id was already used for a different reading')
            return {'duplicate': True, 'incidents': []}
        now, at = self._now(), _time(reading.timestamp)
        age = (now - at).total_seconds()
        if age < -5 or age > 120:
            raise ValueError('Reading is stale or too far in the future')
        stream = f'{reading.board_id}:{reading.metric}:{int(reading.simulation)}'
        previous = self.conn.execute('SELECT body FROM facility_readings WHERE stream=?', (stream,)).fetchone()
        if previous and at <= _time(json.loads(previous[0])['timestamp']):
            raise ValueError('Readings must have increasing timestamps within each stream')
        changes = []
        with self._transaction():
            self._put('facility_events', reading.event_id, payload)
            self.conn.execute('INSERT INTO facility_readings VALUES (?,?) ON CONFLICT(stream) DO UPDATE SET body=excluded.body',
                              (stream, json.dumps(payload, allow_nan=False)))
            hb_key = self._heartbeat_key(reading.board_id, reading.simulation)
            hb = self._get('facility_heartbeats', hb_key)
            if hb is None or at > _time(hb['timestamp']):
                self._put('facility_heartbeats', hb_key, {'board_id': reading.board_id, 'simulation': reading.simulation, 'timestamp': reading.timestamp})
            for rule in self._rules():
                if not rule.enabled or rule.board_id != reading.board_id:
                    continue
                if rule.direction == 'stale':
                    key = self._state_key(rule, reading.simulation)
                    state = self._get('facility_states', key) or {}
                    # A delayed reading must not recover a still-stale board.
                    if state.get('incident_id') and (now - at).total_seconds() < rule.trigger_value:
                        changes.append(self._recover(state['incident_id'], now))
                        self._put('facility_states', key, {})
                elif rule.metric == reading.metric:
                    change = self._evaluate(rule, reading, at)
                    if change:
                        changes.append(change)
        return {'duplicate': False, 'incidents': changes}

    def _evaluate(self, rule, reading, at):
        key = self._state_key(rule, reading.simulation)
        state = self._get('facility_states', key) or {}
        trigger = reading.value > rule.trigger_value if rule.direction == 'above' else reading.value < rule.trigger_value
        reset = reading.value <= rule.reset_value if rule.direction == 'above' else reading.value >= rule.reset_value
        if state.get('incident_id'):
            if reset:
                incident = self._recover(state['incident_id'], at)
                self._put('facility_states', key, {'last_at': reading.timestamp})
                return incident
            state['last_at'] = reading.timestamp
            self._put('facility_states', key, state)
            return None
        if not trigger:
            self._put('facility_states', key, {'last_at': reading.timestamp})
            return None
        gap = (at - _time(state['last_at'])).total_seconds() if state.get('last_at') else math.inf
        if not state.get('started_at') or gap > rule.max_gap_seconds:
            state = {'started_at': reading.timestamp, 'samples': 0}
        state['samples'] += 1
        state['last_at'] = reading.timestamp
        if state['samples'] >= rule.min_samples and (at - _time(state['started_at'])).total_seconds() >= rule.min_duration_seconds:
            incident = self._open(rule, reading.value, reading.simulation, at)
            state = {'incident_id': incident['id'], 'last_at': reading.timestamp}
            self._put('facility_states', key, state)
            return incident
        self._put('facility_states', key, state)
        return None

    def _open(self, rule, value, simulation, at):
        incident = {'id': uuid.uuid4().hex[:16], 'rule_id': rule.id, 'board_id': rule.board_id,
                    'title': rule.title, 'summary': rule.summary, 'severity': rule.severity,
                    'status': 'active', 'value': value, 'simulation': simulation, 'opened_at': _iso(at),
                    'recovered_at': None, 'acknowledged_at': None, 'actions': rule.actions,
                    'action_results': {}}
        self._put('facility_incidents', incident['id'], incident)
        return incident

    def _recover(self, incident_id, at):
        incident = self._get('facility_incidents', incident_id)
        incident.update(status='recovered', recovered_at=_iso(at))
        self._put('facility_incidents', incident_id, incident)
        return incident

    def tick(self, now=None):
        now = self._now(now)
        changes = []
        with self._transaction():
            for rule in self._rules():
                if not rule.enabled or rule.direction != 'stale':
                    continue
                for hb in self._all('facility_heartbeats'):
                    if hb['board_id'] != rule.board_id:
                        continue
                    elapsed = (now - _time(hb['timestamp'])).total_seconds()
                    key = self._state_key(rule, hb['simulation'])
                    state = self._get('facility_states', key) or {}
                    if elapsed >= rule.trigger_value and not state.get('incident_id'):
                        incident = self._open(rule, elapsed, hb['simulation'], now)
                        self._put('facility_states', key, {'incident_id': incident['id']})
                        changes.append(incident)
        return changes

    def acknowledge(self, incident_id):
        incident = self._get('facility_incidents', incident_id)
        if incident is None:
            raise KeyError('Unknown facility incident')
        if incident['acknowledged_at'] is None:
            incident['acknowledged_at'] = _iso(self._now())
            self._put('facility_incidents', incident_id, incident)
        return incident

    def update_action_results(self, incident_id, results):
        incident = self._get('facility_incidents', incident_id)
        if incident is None:
            raise KeyError('Unknown facility incident')
        incident['action_results'].update(results)
        self._put('facility_incidents', incident_id, incident)


class _Savepoint:
    def __init__(self, conn):
        self.conn, self.name = conn, 'facility_' + uuid.uuid4().hex

    def __enter__(self):
        self.conn.execute(f'SAVEPOINT {self.name}')

    def __exit__(self, error_type, error, traceback):
        if error_type:
            self.conn.execute(f'ROLLBACK TO {self.name}')
        self.conn.execute(f'RELEASE {self.name}')
        return False
