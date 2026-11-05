"""Read-only room environment answers and evidence-based meeting advice.

Physical sensor_readings take priority. Explicitly enabled synthetic demo data is
kept separate and labelled in every answer. Patterns are not a forecast or booking.
"""
from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

METRICS = ('temperature_c', 'humidity_pct', 'co2_ppm')
RANGES = {'temperature_c': (-40, 125), 'humidity_pct': (0, 100), 'co2_ppm': (1, 100000)}
MAX_HISTORY_SAMPLES = 200000


def active_dataset(conn, board_id):
    # Existing installations remain read-only and do not initialize demo storage.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='room_demo_datasets'").fetchone():
        return None
    from office.room_demo import active_dataset as lookup
    return lookup(conn, board_id)


class RoomEnvironmentPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    timezone: str = Field(min_length=1, max_length=128)
    fresh_seconds: int = Field(ge=1, le=3600)
    history_days: int = Field(ge=1, le=90)
    min_history_days: int = Field(ge=1, le=90)
    min_samples_per_day: int = Field(ge=3, le=1000)
    min_coverage_minutes: int = Field(ge=15, le=1440)
    min_window_coverage_fraction: float = Field(gt=0, le=1)
    bin_minutes: int = Field(ge=1, le=60)
    hot_temperature_c: float = Field(ge=-40, le=125)
    cold_temperature_c: float = Field(ge=-40, le=125)
    humidity_high_pct: float = Field(ge=0, le=100)
    co2_high_ppm: float = Field(ge=1, le=100000)
    usual_hot_fraction: float = Field(gt=0, le=1)
    near_term_hours: float = Field(gt=0, le=24)
    max_future_days: int = Field(ge=1, le=366)
    max_meeting_hours: float = Field(gt=0, le=24)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError('Room environment timezone must be a valid IANA name') from error
        return value

    @model_validator(mode='after')
    def consistent(self):
        if self.cold_temperature_c >= self.hot_temperature_c:
            raise ValueError('Cold threshold must be below hot threshold')
        if self.min_history_days > self.history_days:
            raise ValueError('Required historical days cannot exceed the history window')
        return self


def _aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Room environment timestamps must include a timezone')
    return value.astimezone(UTC)


def _iso(at):
    return at.astimezone(UTC).isoformat(timespec='milliseconds')


def _valid_sample(kind, value, at):
    try:
        stamp = _aware(at)
        low, high = RANGES[kind]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            return None
        return float(value), stamp
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


class RoomConditions:
    def __init__(self, conn, clock=None, policy_path=None):
        self.conn = conn
        self.clock = clock or (lambda: datetime.now(UTC))
        path = Path(policy_path) if policy_path else Path(__file__).resolve().parents[3] / 'config/room-environment.json'
        self.policy = RoomEnvironmentPolicy.model_validate(json.loads(path.read_text()))
        self.zone = ZoneInfo(self.policy.timezone)

    def _now(self):
        return _aware(self.clock())

    def snapshot(self, board_id: str | None):
        if board_id is not None and (not isinstance(board_id, str) or not board_id.strip() or len(board_id) > 64):
            raise ValueError('Board mapping must be a nonblank string of at most 64 characters or null')
        now, readings = self._now(), []
        for kind in METRICS:
            chosen = None
            if board_id is not None:
                # SQLite's julianday normalizes offsets before ordering. Python
                # then rejects naive or invalid timestamps and implausible values.
                cursor = self.conn.execute('''SELECT value,at FROM sensor_readings WHERE board_id=? AND kind=?
                    ORDER BY julianday(at) DESC,rowid DESC''', (board_id, kind))
                for row in cursor:
                    valid = _valid_sample(kind, row[0], row[1])
                    if valid is not None:
                        chosen = valid
                        break
            if chosen is None:
                readings.append({'kind': kind, 'value': None, 'at': None, 'state': 'missing', 'last_value': None})
                continue
            value, at = chosen
            fresh = 0 <= (now - at).total_seconds() <= self.policy.fresh_seconds
            readings.append({'kind': kind, 'value': value if fresh else None, 'at': _iso(at),
                             'state': 'fresh' if fresh else 'stale', 'last_value': value})
        result = {'evaluated_at': _iso(now), 'board_id': board_id, 'readings': readings,
                  'basis': 'physical', 'simulation': False}
        # A demo preset never becomes a physical sample or a fresh observation.
        # Partial live data stays partial; sources are not blended into one room.
        dataset = active_dataset(self.conn, board_id)
        if dataset and not any(r['state'] == 'fresh' for r in readings):
            result.update(basis='synthetic', simulation=True)
            result['readings'] = [{'kind': kind, 'value': dataset['current_values'][kind],
                'last_value': dataset['current_values'][kind], 'at': dataset['seeded_at'], 'state': 'demo'} for kind in METRICS]
        return result

    def _history(self, board_id, start, end, now):
        physical = self._history_source(board_id, start, end, now)
        if physical['state'] != 'insufficient':
            return physical
        dataset = active_dataset(self.conn, board_id)
        return self._history_source(board_id, start, end, now, dataset) if dataset else physical

    def _history_source(self, board_id, start, end, now, dataset=None):
        p, today = self.policy, now.astimezone(self.zone).date()
        history = {'state': 'insufficient', 'days': 0, 'hot_days': 0, 'hot_fraction': None,
                   'required_days': p.min_history_days, 'timezone': p.timezone,
                   'window_days': p.history_days, 'weekday_class': 'weekday' if start.astimezone(self.zone).weekday() < 5 else 'weekend',
                   'qualified_days': [], 'reason': 'No mapped room sensor history is available',
                   'basis': 'synthetic' if dataset else 'physical' if board_id else 'none',
                   'simulation': bool(dataset), 'dataset_id': dataset['dataset_id'] if dataset else None}
        if board_id is None:
            return history
        local_start, local_end = start.astimezone(self.zone), end.astimezone(self.zone)
        date_span = (local_end.date() - local_start.date()).days
        first = today - timedelta(days=p.history_days)
        target_weekday = local_start.weekday() < 5
        windows = []
        day = first
        while day < today:
            if (day.weekday() < 5) == target_weekday:
                a = datetime.combine(day, local_start.timetz().replace(tzinfo=None), self.zone).replace(fold=local_start.fold)
                b = datetime.combine(day + timedelta(days=date_span), local_end.timetz().replace(tzinfo=None), self.zone).replace(fold=local_end.fold)
                if b.timestamp() > a.timestamp():
                    windows.append((day, a.timestamp(), b.timestamp()))
            day += timedelta(days=1)
        days = {}
        duration_minutes = (end - start).total_seconds() / 60
        required_coverage = max(p.min_coverage_minutes, duration_minutes * p.min_window_coverage_fraction)
        required_bins = math.ceil(duration_minutes / p.bin_minutes * p.min_window_coverage_fraction)
        if not windows:
            return history
        # Bound the analysis to relevant local-time windows before applying the
        # sample cap. An always-on sensor's unrelated all-day samples must not
        # crowd out a month's comparable meeting-time observations.
        clauses = ' OR '.join('(julianday(at)>=julianday(?) AND julianday(at)<julianday(?))' for _ in windows)
        limits = [bound for _, a, b in windows for bound in
                  (_iso(datetime.fromtimestamp(a, UTC)), _iso(datetime.fromtimestamp(b, UTC)))]
        source = 'room_demo_samples' if dataset else 'sensor_readings'
        scope = "dataset_id=? AND metric='temperature_c'" if dataset else "board_id=? AND kind='temperature_c'"
        source_id = dataset['dataset_id'] if dataset else board_id
        rows = self.conn.execute(f'''SELECT value,at FROM {source} WHERE {scope}
            AND ({clauses}) ORDER BY julianday(at) LIMIT ?''', (source_id, *limits, MAX_HISTORY_SAMPLES + 1))
        count = 0
        for row in rows:
            count += 1
            if count > MAX_HISTORY_SAMPLES:
                return history | {'reason': 'Historical sample density exceeds the bounded analysis limit'}
            valid = _valid_sample('temperature_c', row[0], row[1])
            if valid is None:
                continue
            value, at = valid
            local = at.astimezone(self.zone)
            if local.date() >= today:
                continue
            for date, a, b in windows:
                if a <= at.timestamp() < b:
                    key = date.isoformat()
                    bucket = int((local.hour * 60 + local.minute) // p.bin_minutes)
                    # Date is part of the bin key for meetings crossing midnight;
                    # folds share one wall-clock bin so DST repetition adds no weight.
                    bucket_key = (local.date().isoformat(), bucket)
                    state = days.setdefault(key, {'samples': 0, 'first': at, 'last': at, 'bins': {}})
                    state['samples'] += 1
                    state['first'], state['last'] = min(state['first'], at), max(state['last'], at)
                    total, n = state['bins'].get(bucket_key, (0.0, 0))
                    state['bins'][bucket_key] = (total + value, n + 1)
                    break
        qualified = []
        for date, state in sorted(days.items()):
            coverage = (state['last'] - state['first']).total_seconds() / 60
            if state['samples'] >= p.min_samples_per_day and coverage >= required_coverage and len(state['bins']) >= required_bins:
                mean = sum(total / n for total, n in state['bins'].values()) / len(state['bins'])
                qualified.append({'date': date, 'average_temperature_c': mean, 'samples': state['samples'],
                                  'bins': len(state['bins']), 'coverage_minutes': coverage, 'hot': mean > p.hot_temperature_c})
        total, hot = len(qualified), sum(day['hot'] for day in qualified)
        fraction = hot / total if total else None
        state = 'insufficient' if total < p.min_history_days else 'usually_hot' if fraction >= p.usual_hot_fraction else 'no_hot_pattern'
        reason = f'{total} distinct prior days meet sample and coverage requirements'
        return history | {'state': state, 'days': total, 'hot_days': hot, 'hot_fraction': fraction,
                          'qualified_days': qualified, 'reason': reason}

    def assess(self, board_id, starts_at, ends_at):
        now, start, end = self._now(), _aware(starts_at), _aware(ends_at)
        # A 'now' request can arrive after its second-resolution start. Allow
        # five minutes of request latency while requiring a future meeting end.
        if start < now - timedelta(minutes=5) or start - now > timedelta(days=self.policy.max_future_days):
            raise ValueError('Meeting start is outside the allowed now/future horizon')
        if end <= now or end <= start or end - start > timedelta(hours=self.policy.max_meeting_hours):
            raise ValueError('Meeting end must follow its start within the configured duration limit')
        current = self.snapshot(board_id)
        history = self._history(board_id, start, end, now)
        values = {r['kind']: r['value'] for r in current['readings']}
        p = self.policy
        problems = []
        if values['temperature_c'] is not None and values['temperature_c'] > p.hot_temperature_c:
            problems.append('high temperature')
        if values['humidity_pct'] is not None and values['humidity_pct'] > p.humidity_high_pct:
            problems.append('high humidity')
        if values['co2_ppm'] is not None and values['co2_ppm'] > p.co2_high_ppm:
            problems.append('elevated CO₂')
        cold = values['temperature_c'] is not None and values['temperature_c'] < p.cold_temperature_c
        near = start - now <= timedelta(hours=p.near_term_hours)
        if history['state'] == 'usually_hot':
            recommendation = 'avoid'
            summary = f"Consider another room: {history['hot_days']} of {history['days']} qualified prior days were hot at this local meeting time."
        elif near and problems:
            recommendation = 'avoid'
            summary = 'Consider another room for this near-term meeting: current readings show ' + ', '.join(problems) + '.'
        elif problems or cold:
            recommendation = 'caution'
            summary = 'Current readings show ' + ', '.join(problems + (['low temperature'] if cold else [])) + '; check conditions again before the meeting.'
        elif any(value is None for value in values.values()):
            recommendation = 'unknown'
            summary = 'Fresh readings are incomplete for this room; meeting conditions cannot be assessed fully.'
        elif not near and history['state'] == 'insufficient':
            recommendation = 'unknown'
            summary = 'Current readings are within configured bands; there is insufficient same-time history for the future meeting.'
        else:
            recommendation = 'suitable'
            summary = 'Current readings are within configured bands' + ('; qualified same-time history shows no recurring heat pattern.' if history['state'] == 'no_hot_pattern' else '; same-time history is insufficient to establish a pattern.')
        if history['simulation']:
            summary = 'Synthetic demo history: ' + summary
        if values['temperature_c'] is not None:
            prefix = 'Synthetic demo temperature:' if current['simulation'] else 'It is'
            suffix = '.' if current['simulation'] else ' now.'
            summary = f"{prefix} {values['temperature_c']:g} °C{suffix} " + summary
        return {'current': current, 'history': history, 'recommendation': recommendation, 'summary': summary}


_METRIC_PATTERNS = {
    'temperature_c': re.compile(r'\b(?:temperature|temperatures|temperatur(?:en)?|temp|hot|cold|warm|varm(?:t)?|kald(?:t)?)\b', re.I),
    'humidity_pct': re.compile(r'\b(?:humidity|humid|luftfuktighet(?:en)?|fuktighet(?:en)?|fuktig)\b', re.I),
    'co2_ppm': re.compile(r'\b(?:co[2₂]|carbon\s+dioxide|karbondioksid)\b', re.I),
}
_AIR = re.compile(r'\b(?:air\s*quality|room\s+conditions|room\s+environment|luftkvalitet(?:en)?|luft(?:a|en)?)\b|\bair\b(?=\s*(?:[?.!]|$|in\b|here\b|now\b))', re.I)
_INSTRUCTION = re.compile(r'\b(?:book(?:ing|e)?|reserve|reservation|schedule|bestill(?:e|ing)?|reserver(?:e|ing)?|reservasjon|order|purchase|buy|replenish|fix|repair|inspect|service|ventilate|open|close|turn|set|lower|increase|decrease|reduce|kjøp|etterfyll|fiks|reparer|kontroller|åpne|lukk|skru|senk|øk|varsle)\b', re.I)
_IMPORTANT = re.compile(r'\b(?:important|viktig(?:e)?)\b', re.I)
_MEETING = re.compile(r'\b(?:meeting|møte(?:t|r)?)\b', re.I)
_QUESTION = re.compile(r'[?]|\b(?:what|how|whats|hva|hvordan|hvor)\b|^\s*(?:is|are|er|har|tell|show|vis|si|les|read|check|sjekk)\b', re.I)


def condition_question(text) -> bool:
    if not isinstance(text, str) or not text.strip() or len(text) > 2000 or _INSTRUCTION.search(text):
        return False
    if _IMPORTANT.search(text) and _MEETING.search(text):
        return False
    has_metric = any(pattern.search(text) for pattern in _METRIC_PATTERNS.values())
    if not has_metric and not _AIR.search(text):
        return False
    if _QUESTION.search(text):
        return True
    remaining = _AIR.sub('', text)
    for pattern in _METRIC_PATTERNS.values():
        remaining = pattern.sub('', remaining)
    return re.sub(r'\b(?:and|og|the|please|takk)\b|[\s,;:.!]+', '', remaining, flags=re.I) == ''


def format_readings(snapshot, text='') -> str:
    requested = list(METRICS) if _AIR.search(text) else [kind for kind, pattern in _METRIC_PATTERNS.items() if pattern.search(text)]
    requested = requested or list(METRICS)
    norwegian = bool(re.search(r'\b(?:hva|hvordan|hvor|temperatur(?:en)?|luftfuktighet(?:en)?|fuktighet(?:en)?|luftkvalitet(?:en)?)\b', text, re.I))
    labels = {'temperature_c': 'Temperatur' if norwegian else 'Temperature',
              'humidity_pct': 'Luftfuktighet' if norwegian else 'Humidity', 'co2_ppm': 'CO₂'}
    units = {'temperature_c': '°C', 'humidity_pct': '% RH', 'co2_ppm': 'ppm'}
    readings = {r['kind']: r for r in snapshot.get('readings', [])}
    parts = []
    for kind in requested:
        sample = readings.get(kind, {})
        value = sample.get('value')
        if sample.get('state') in {'fresh', 'demo'} and type(value) in (int, float) and math.isfinite(value):
            answer = f'{value:g} {units[kind]}'
        else:
            answer = 'ingen fersk måling' if norwegian else 'no fresh reading'
        parts.append(f'{labels[kind]}: {answer}')
    prefix = 'Room sensor mapping is unknown. ' if snapshot.get('board_id') is None else ''
    if snapshot.get('simulation') or any(r.get('state') == 'demo' for r in snapshot.get('readings', [])):
        prefix = 'Demodata (syntetisk): ' if norwegian else 'Synthetic demo readings: '
    return prefix + '; '.join(parts) + '.'
