from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from office import db
from office.facility_rules import FacilityEngine, FacilityReading, FacilityRule


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 5, 12, tzinfo=UTC)

    def __call__(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)


@pytest.fixture
def setup():
    conn, clock = db.connect(':memory:'), Clock()
    engine = FacilityEngine(conn, clock=clock)
    yield conn, clock, engine
    conn.close()


def reading(clock, value=1500.0, metric='co2_ppm', simulation=False, event_id=None, at=None):
    return FacilityReading(event_id=event_id or f'{metric}:{simulation}:{clock.at.timestamp()}',
                           board_id='kitchen', metric=metric, value=value,
                           timestamp=(at or clock.at).isoformat(), simulation=simulation)


def get_rule(engine, id='co2-high'):
    return FacilityRule.model_validate(next(r for r in engine.snapshot()['rules'] if r['id'] == id))


def open_co2(clock, engine, simulation=False):
    assert engine.ingest(reading(clock, simulation=simulation))['incidents'] == []
    clock.advance(15)
    assert engine.ingest(reading(clock, simulation=simulation))['incidents'] == []
    clock.advance(15)
    result = engine.ingest(reading(clock, simulation=simulation))
    assert len(result['incidents']) == 1
    return result['incidents'][0]


def test_sustained_threshold_hysteresis_and_acknowledgement(setup):
    _, clock, engine = setup
    incident = open_co2(clock, engine)
    assert incident['status'] == 'active'
    assert incident['actions'] == ['alert', 'ticket']
    clock.advance(5)
    assert engine.ingest(reading(clock, value=1100.0))['incidents'] == []
    assert engine.acknowledge(incident['id'])['acknowledged_at']
    clock.advance(5)
    recovered = engine.ingest(reading(clock, value=1000.0))['incidents'][0]
    assert recovered['status'] == 'recovered'
    assert recovered['acknowledged_at']
    clock.advance(5)
    assert open_co2(clock, engine)['id'] != incident['id']


def test_duplicate_and_conflicting_event_payload(setup):
    _, clock, engine = setup
    first = reading(clock, event_id='same-event')
    assert engine.ingest(first)['duplicate'] is False
    clock.advance(500)
    assert engine.ingest(first) == {'duplicate': True, 'incidents': []}
    with pytest.raises(ValueError, match='different reading'):
        engine.ingest(first.model_copy(update={'value': 2000.0}))
    assert len(engine.snapshot()['readings']) == 1


def test_debounce_gap_and_intervening_good_reading_reset_candidates(setup):
    _, clock, engine = setup
    engine.ingest(reading(clock))
    clock.advance(40)
    assert engine.ingest(reading(clock))['incidents'] == []
    clock.advance(15)
    assert engine.ingest(reading(clock))['incidents'] == []
    clock.advance(15)
    assert engine.ingest(reading(clock, value=1100.0))['incidents'] == []
    clock.advance(1)
    assert open_co2(clock, engine)['status'] == 'active'


def test_candidate_and_latch_survive_process_restart(setup):
    conn, clock, engine = setup
    engine.ingest(reading(clock))
    clock.advance(15)
    engine.ingest(reading(clock))
    restarted = FacilityEngine(conn, clock=clock)
    clock.advance(15)
    incident = restarted.ingest(reading(clock))['incidents'][0]
    another = FacilityEngine(conn, clock=clock)
    clock.advance(20)
    assert another.ingest(reading(clock))['incidents'] == []
    assert another.snapshot()['incidents'][0]['id'] == incident['id']


def test_stale_future_and_out_of_order_rejected_atomically(setup):
    _, clock, engine = setup
    engine.ingest(reading(clock))
    for at in (clock.at - timedelta(seconds=121), clock.at + timedelta(seconds=6), clock.at):
        with pytest.raises(ValueError):
            engine.ingest(reading(clock, at=at, event_id=f'bad-{at.isoformat()}'))
    assert len(engine.snapshot()['readings']) == 1
    assert engine.snapshot()['incidents'] == []


def test_live_and_simulated_samples_never_combine_or_resolve_one_another(setup):
    _, clock, engine = setup
    engine.ingest(reading(clock))
    clock.advance(15)
    engine.ingest(reading(clock, simulation=True))
    clock.advance(15)
    assert engine.ingest(reading(clock))['incidents'] == []
    clock.advance(15)
    assert engine.ingest(reading(clock))['incidents'][0]['simulation'] is False
    clock.advance(1)
    assert engine.ingest(reading(clock, value=800.0, simulation=True))['incidents'] == []
    assert [i['status'] for i in engine.snapshot()['incidents']] == ['active']
    clock.advance(1)
    assert engine.ingest(reading(clock, value=800.0))['incidents'][0]['status'] == 'recovered'


def test_heartbeat_opens_once_and_recovers_only_from_its_own_domain(setup):
    _, clock, engine = setup
    clock.advance(30)
    incident = engine.tick()[0]
    assert incident['rule_id'] == 'telemetry-offline'
    assert engine.tick() == []
    engine.ingest(reading(clock, metric='temperature_c', value=22.0, simulation=True))
    assert engine.snapshot()['incidents'][0]['status'] == 'active'
    clock.advance(1)
    recovered = engine.ingest(reading(clock, metric='temperature_c', value=22.0))['incidents'][0]
    assert recovered['id'] == incident['id']
    assert recovered['status'] == 'recovered'
    clock.advance(30)
    assert len(engine.tick()) == 2  # separate simulated and live offline streams


def test_delayed_reading_cannot_clear_offline_incident(setup):
    _, clock, engine = setup
    clock.advance(60)
    incident = engine.tick()[0]
    result = engine.ingest(reading(clock, at=clock.at - timedelta(seconds=40)))
    assert result['incidents'] == []
    assert engine.snapshot()['incidents'][0]['id'] == incident['id']
    assert engine.snapshot()['incidents'][0]['status'] == 'active'


def test_configuration_persists_validates_and_preserves_active_episode(setup):
    conn, clock, engine = setup
    incident = open_co2(clock, engine)
    rule = get_rule(engine).model_copy(update={'trigger_value': 1400.0, 'actions': ['alert']})
    assert engine.save_rule(rule)['trigger_value'] == 1400.0
    engine.update_action_results(incident['id'], {'ticket': {'id': 'local-ticket'}})
    snapshot = FacilityEngine(conn, clock=clock).snapshot()
    assert next(r for r in snapshot['rules'] if r['id'] == rule.id)['actions'] == ['alert']
    assert snapshot['incidents'][0]['status'] == 'active'
    assert snapshot['incidents'][0]['actions'] == ['alert', 'ticket']
    assert snapshot['incidents'][0]['recovered_at'] is None
    assert snapshot['incidents'][0]['action_results']['ticket']['id'] == 'local-ticket'
    with pytest.raises(ValueError, match='immutable'):
        engine.save_rule(rule.model_copy(update={'board_id': 'printer'}))
    with pytest.raises(ValidationError):
        engine.save_rule(rule.model_copy(update={'reset_value': 1500.0}))
    with pytest.raises(ValidationError):
        engine.save_rule(rule.model_copy(update={'actions': ['replenish']}))


def test_percent_nan_distance_and_unsafe_action_validation(setup):
    _, clock, engine = setup
    for metric, value in [('stock_level', 101.0), ('obstruction_mm', 32767.0),
                          ('humidity_pct', -1.0), ('co2_ppm', float('nan'))]:
        with pytest.raises(ValidationError):
            reading(clock, metric=metric, value=value)
    rule = get_rule(engine, 'noise-high')
    with pytest.raises(ValidationError):
        engine.save_rule(rule.model_copy(update={'actions': ['alert', 'sound']}))
    with pytest.raises(ValidationError):
        engine.save_rule(rule.model_copy(update={'min_samples': 0}))
    with pytest.raises(ValidationError):
        reading(clock).model_validate(reading(clock).model_dump() | {'simulation': 'false'})


def test_stock_and_disabled_alternative_distance_rules(setup):
    _, clock, engine = setup
    for seconds in (0, 15, 15):
        clock.advance(seconds)
        result = engine.ingest(reading(clock, metric='stock_level', value=10.0))
    incident = result['incidents'][0]
    assert incident['rule_id'] == 'stock-low'
    assert incident['actions'] == ['alert', 'replenish']
    clock.advance(1)
    engine.ingest(reading(clock, metric='bin_fill_pct', value=95.0))
    assert len(engine.snapshot()['incidents']) == 1


def test_savepoints_do_not_commit_callers_transaction(setup):
    conn, clock, engine = setup
    conn.execute('BEGIN')
    engine.ingest(reading(clock))
    conn.execute('ROLLBACK')
    assert engine.snapshot()['readings'] == []


def test_disabling_rule_pauses_without_false_recovery_or_duplicate(setup):
    conn, clock, engine = setup
    incident = open_co2(clock, engine)
    original = get_rule(engine)
    disabled = original.model_copy(update={'enabled': False, 'actions': ['alert']})
    engine.save_rule(disabled)
    clock.advance(1)
    assert engine.ingest(reading(clock, value=800.0))['incidents'] == []
    persisted = FacilityEngine(conn, clock=clock)
    current = persisted.snapshot()['incidents'][0]
    assert current['id'] == incident['id']
    assert current['status'] == 'active'
    assert current['recovered_at'] is None
    assert current['actions'] == original.actions
    assert persisted.acknowledge(incident['id'])['acknowledged_at']
    persisted.save_rule(disabled.model_copy(update={'enabled': True}))
    for seconds in (1, 15, 15):
        clock.advance(seconds)
        assert persisted.ingest(reading(clock))['incidents'] == []
    assert len(persisted.snapshot()['incidents']) == 1
    clock.advance(1)
    recovered = persisted.ingest(reading(clock, value=1000.0))['incidents'][0]
    assert recovered['id'] == incident['id']
    assert recovered['status'] == 'recovered'
    assert recovered['actions'] == original.actions


def test_config_edit_discards_candidate_samples_without_creating_incident(setup):
    _, clock, engine = setup
    engine.ingest(reading(clock))
    clock.advance(15)
    engine.ingest(reading(clock))
    engine.save_rule(get_rule(engine).model_copy(update={'actions': ['alert']}))
    clock.advance(15)
    assert engine.ingest(reading(clock))['incidents'] == []
    clock.advance(15)
    assert engine.ingest(reading(clock))['incidents'] == []
    clock.advance(15)
    incident = engine.ingest(reading(clock))['incidents'][0]
    assert incident['actions'] == ['alert']
