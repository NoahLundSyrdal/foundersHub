from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from office import db
from office.room_conditions import RoomConditions, condition_question, format_readings

OSLO = ZoneInfo('Europe/Oslo')


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 5, 11, tzinfo=UTC)

    def __call__(self):
        return self.at


@pytest.fixture
def setup():
    conn, clock = db.connect(':memory:'), Clock()
    yield conn, clock, RoomConditions(conn, clock)
    conn.close()


def insert(conn, at, value=22, kind='temperature_c', board='room-a'):
    conn.execute('INSERT INTO sensor_readings VALUES(?,?,?,?)', (board, kind, value, at.isoformat() if isinstance(at, datetime) else at))


def current(conn, clock, board='room-a', temperature=22, humidity=50, co2=800):
    for metric, value in [('temperature_c', temperature), ('humidity_pct', humidity), ('co2_ppm', co2)]:
        if value is not None:
            insert(conn, clock.at, value, metric, board)


def meeting(day='2026-11-06', start='13:00', end='14:00'):
    return datetime.fromisoformat(f'{day}T{start}').replace(tzinfo=OSLO), datetime.fromisoformat(f'{day}T{end}').replace(tzinfo=OSLO)


def history_day(conn, date, values=(30, 30, 30), board='room-a', minutes=(0, 15, 30), hour=13):
    for minute, value in zip(minutes, values):
        at = datetime.fromisoformat(f'{date}T{hour:02d}:00').replace(tzinfo=OSLO) + timedelta(minutes=minute)
        insert(conn, at, value, board=board)


def test_snapshot_latest_is_chronological_aware_and_board_scoped(setup):
    conn, clock, checks = setup
    insert(conn, '2026-11-05T13:59:00+02:00', 23)  # 11:59 UTC is future at clock11:00
    insert(conn, '2026-11-05T11:58:00Z', 19)
    clock.at = datetime(2026, 11, 5, 12, tzinfo=UTC)
    result = checks.snapshot('room-a')['readings'][0]
    assert result['value'] == 23 and result['state'] == 'fresh'
    insert(conn, clock.at - timedelta(seconds=80), 20)
    assert checks.snapshot('room-a')['readings'][0]['value'] == 23
    current(conn, clock, board='kitchen', temperature=99)
    assert checks.snapshot('room-a')['readings'][0]['value'] == 23
    assert all(r['state'] == 'missing' for r in checks.snapshot(None)['readings'])
    assert all(r['value'] is None for r in checks.snapshot('unmapped-room')['readings'])


def test_missing_stale_future_invalid_and_alias_readings_never_appear_current(setup):
    conn, clock, checks = setup
    insert(conn, clock.at - timedelta(seconds=121), 24)
    insert(conn, clock.at, 900, 'co2_co2')  # only canonical rows are authoritative
    insert(conn, '2026-11-05T11:00:00', 88, 'humidity_pct')  # naive timestamp rejected
    readings = {r['kind']: r for r in checks.snapshot('room-a')['readings']}
    assert readings['temperature_c']['state'] == 'stale'
    assert readings['temperature_c']['value'] is None and readings['temperature_c']['last_value'] == 24
    assert readings['co2_ppm']['state'] == readings['humidity_pct']['state'] == 'missing'
    insert(conn, clock.at + timedelta(seconds=1), 26)
    assert checks.snapshot('room-a')['readings'][0]['state'] == 'stale'
    answer = format_readings(checks.snapshot('room-a'), 'What is the temperature?')
    assert 'no fresh reading' in answer and '26' not in answer and '24' not in answer
    unknown = format_readings(checks.snapshot(None))
    assert 'unknown' in unknown and '0 ' not in unknown


def test_formatter_reports_requested_metrics_and_units_without_fake_values(setup):
    conn, clock, checks = setup
    current(conn, clock, temperature=22.5, humidity=45, co2=987)
    answer = format_readings(checks.snapshot('room-a'), 'What is the temperature and humidity?')
    assert '22.5 °C' in answer and '45 % RH' in answer and 'ppm' not in answer
    assert 'CO₂: 987 ppm' in format_readings(checks.snapshot('room-a'), 'How is the air quality?')
    assert 'Temperatur: 22.5 °C' in format_readings(checks.snapshot('room-a'), 'Hva er temperaturen?')
    assert '22.5 °C' in format_readings(checks.snapshot('room-a'), 'How hot is it in here?')
    assert 'ppm' not in format_readings(checks.snapshot('room-a'), 'Is it cold?')
    assert 'CO₂: 987 ppm' in format_readings(checks.snapshot('room-a'), 'How is the air?')
    assert '45 % RH' in format_readings(checks.snapshot('room-a'), 'What are the room conditions?')


@pytest.mark.parametrize('text', ['What is the temperature?', 'How humid is it, humidity?', 'Humidity',
    'temperature and humidity', 'Hva er temperaturen?', 'Hva er luftfuktigheten?', 'Er CO₂ høy?',
    'How is the air quality?', 'Sjekk luftkvaliteten', 'How hot is it in here?',
    'What are the room conditions?', 'How is the air?', 'Is it cold?', 'Er det varmt her?'])
def test_condition_questions_match_narrowly(text):
    assert condition_question(text)


@pytest.mark.parametrize('text', ['Book a meeting room and check temperature', 'Reserve room because CO2 is high',
    'I have an important meeting tomorrow; is the temperature usually high?',
    'Jeg har et viktig møte i morgen, hva er temperaturen?', 'Order a humidity sensor',
    'Fix the air quality', 'Åpne vinduet fordi CO2 er høy', 'Set temperature to 20',
    'Is the printer working?', 'Is the air conditioning broken?', 'We are out of coffee', 'Can you inspect the ventilation?', 'temperature is rising'])
def test_actions_and_meeting_plans_are_not_hijacked_as_reading_questions(text):
    assert not condition_question(text)


def test_three_distinct_qualified_days_establish_pattern_without_prediction(setup):
    conn, clock, checks = setup
    current(conn, clock)
    for day, values in [('2026-11-02', (30, 30, 30)), ('2026-11-03', (30, 30, 30)), ('2026-11-04', (20, 20, 20))]:
        history_day(conn, day, values)
    result = checks.assess('room-a', *meeting())
    assert result['history']['state'] == 'usually_hot'
    assert result['history']['days'] == 3 and result['history']['hot_days'] == 2
    assert result['recommendation'] == 'avoid'
    assert 'prior days' in result['summary'] and '22 °C now' in result['summary']


def test_insufficient_history_never_claims_usually_or_normally(setup):
    conn, clock, checks = setup
    current(conn, clock)
    history_day(conn, '2026-11-03')
    history_day(conn, '2026-11-04')
    result = checks.assess('room-a', *meeting())
    assert result['history']['state'] == 'insufficient' and result['history']['days'] == 2
    assert result['recommendation'] == 'unknown'
    assert 'usually' not in result['summary'] and 'normally' not in result['summary']


def test_history_requires_samples_and_coverage_on_each_distinct_day(setup):
    conn, clock, checks = setup
    current(conn, clock)
    history_day(conn, '2026-11-02', minutes=(0, 1, 2))  # insufficient coverage
    history_day(conn, '2026-11-03', values=(30, 30), minutes=(0, 30))  # insufficient samples
    history_day(conn, '2026-11-04')
    for second in range(100):
        at = datetime(2026, 11, 4, 13, 0, second % 60, tzinfo=OSLO)
        insert(conn, at, 30)
    result = checks.assess('room-a', *meeting())
    assert result['history']['days'] == 1
    assert result['history']['state'] == 'insufficient'


def test_fifteen_minute_bins_and_days_are_equally_weighted_despite_bursts(setup):
    conn, clock, checks = setup
    current(conn, clock)
    for date in ('2026-11-02', '2026-11-03', '2026-11-04'):
        history_day(conn, date, (40, 20, 20))
        for second in range(100):
            insert(conn, datetime.fromisoformat(f'{date}T13:00:00').replace(tzinfo=OSLO) + timedelta(seconds=second), 40)
    history = checks.assess('room-a', *meeting())['history']
    assert history['state'] == 'no_hot_pattern' and history['hot_days'] == 0
    assert all(day['average_temperature_c'] == pytest.approx(80 / 3) for day in history['qualified_days'])


def test_wrong_time_today_weekend_old_history_and_other_boards_are_excluded(setup):
    conn, clock, checks = setup
    current(conn, clock)
    for date in ('2026-11-02', '2026-11-03', '2026-11-04'):
        history_day(conn, date, hour=9)  # wrong same-time window
        history_day(conn, date, board='kitchen')
    history_day(conn, '2026-10-31')  # Saturday, target Friday is weekday
    history_day(conn, '2026-11-01')  # Sunday
    history_day(conn, '2026-10-01')  # outside 28 days
    history_day(conn, '2026-11-05')  # today, excluded even if we move now after this time
    clock.at = datetime(2026, 11, 5, 16, tzinfo=UTC)
    result = checks.assess('room-a', *meeting())
    assert result['history']['days'] == 0 and result['history']['state'] == 'insufficient'
    assert checks.assess(None, *meeting())['recommendation'] == 'unknown'


def test_current_bad_near_term_avoids_but_future_only_cautions(setup):
    conn, clock, checks = setup
    current(conn, clock, temperature=29)
    near = checks.assess('room-a', clock.at + timedelta(hours=1), clock.at + timedelta(hours=2))
    future = checks.assess('room-a', *meeting())
    assert near['recommendation'] == 'avoid'
    assert future['recommendation'] == 'caution'
    assert future['history']['state'] == 'insufficient'


@pytest.mark.parametrize('changes', [dict(humidity=80), dict(co2=1600)])
def test_high_humidity_or_co2_advise_avoid_for_near_term(setup, changes):
    conn, clock, checks = setup
    current(conn, clock, **changes)
    result = checks.assess('room-a', clock.at + timedelta(hours=1), clock.at + timedelta(hours=2))
    assert result['recommendation'] == 'avoid'


def test_cold_cautions_and_near_term_complete_good_readings_are_suitable(setup):
    conn, clock, checks = setup
    current(conn, clock, temperature=16)
    assert checks.assess('room-a', clock.at, clock.at + timedelta(hours=1))['recommendation'] == 'caution'
    clock.at += timedelta(seconds=1)
    current(conn, clock)
    result = checks.assess('room-a', clock.at, clock.at + timedelta(hours=1))
    assert result['recommendation'] == 'suitable'
    assert result['history']['state'] == 'insufficient'


def test_time_bounds_allow_now_latency_but_reject_bad_intervals(setup):
    conn, clock, checks = setup
    current(conn, clock)
    assert checks.assess('room-a', clock.at - timedelta(seconds=1), clock.at + timedelta(minutes=30))['recommendation'] == 'suitable'
    for start, end in [(clock.at - timedelta(minutes=6), clock.at + timedelta(hours=1)),
                       (clock.at, clock.at), (clock.at, clock.at - timedelta(seconds=1)),
                       (clock.at + timedelta(days=367), clock.at + timedelta(days=368)),
                       (clock.at, clock.at + timedelta(hours=25)),
                       (clock.at.replace(tzinfo=None), clock.at + timedelta(hours=1))]:
        with pytest.raises(ValueError):
            checks.assess('room-a', start, end)


def test_local_same_time_history_matches_across_oslo_dst_change(setup):
    conn, clock, checks = setup
    clock.at = datetime(2026, 10, 30, 9, tzinfo=UTC)
    current(conn, clock)
    for date in ('2026-10-22', '2026-10-23', '2026-10-26'):
        # 13:00 Oslo maps to UTC11 before DST change and UTC12 after it.
        for minutes in (0, 15, 30):
            local = datetime.fromisoformat(f'{date}T13:00:00').replace(tzinfo=OSLO) + timedelta(minutes=minutes)
            insert(conn, local.astimezone(UTC), 30)
    result = checks.assess('room-a', *meeting('2026-10-30'))
    assert result['history']['days'] == 3 and result['history']['state'] == 'usually_hot'


def test_component_is_read_only_and_config_is_validated(setup, tmp_path):
    conn, clock, checks = setup
    current(conn, clock)
    changes = conn.total_changes
    checks.snapshot('room-a')
    checks.assess('room-a', *meeting())
    assert conn.total_changes == changes
    policy = json.loads((Path(__file__).resolve().parents[3] / 'config/room-environment.json').read_text())
    for update in ({'timezone': 'Bad/Zone'}, {'fresh_seconds': True}, {'extra': 'no'}, {'cold_temperature_c': 40}):
        path = tmp_path / 'invalid.json'
        path.write_text(json.dumps(policy | update))
        with pytest.raises(ValidationError):
            RoomConditions(conn, clock, policy_path=path)


def test_short_observation_cannot_establish_a_pattern_for_a_long_meeting(setup):
    conn, _, checks = setup
    for date in ('2026-11-02', '2026-11-03', '2026-11-04'):
        history_day(conn, date)
    result = checks.assess('room-a', *meeting(end='17:00'))
    assert result['history']['state'] == 'insufficient' and result['history']['days'] == 0


def test_irrelevant_all_day_samples_cannot_exhaust_meeting_history_limit(setup, monkeypatch):
    from office import room_conditions
    conn, _, checks = setup
    monkeypatch.setattr(room_conditions, 'MAX_HISTORY_SAMPLES', 12)
    for date in ('2026-11-02', '2026-11-03', '2026-11-04'):
        history_day(conn, date)
        for minute in range(30):
            insert(conn, datetime.fromisoformat(f'{date}T08:00').replace(tzinfo=OSLO) + timedelta(minutes=minute), 22)
    result = checks.assess('room-a', *meeting())
    assert result['history']['state'] == 'usually_hot' and result['history']['days'] == 3
