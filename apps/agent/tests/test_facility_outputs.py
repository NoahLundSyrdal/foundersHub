from concurrent.futures import ThreadPoolExecutor

import pytest

from office import db
from office.facility_outputs import StationActions


@pytest.fixture
def setup():
    conn = db.connect(':memory:')
    yield conn, StationActions(conn)
    conn.close()


def incident(id='incident-one', board_id='kitchen', **extra):
    return {'id': id, 'board_id': board_id, 'simulation': False, 'title': 'Object left in monitored space',
            'status': 'active', 'actions': ['alert', 'sound']} | extra


def test_enqueue_duplicate_restart_and_claim_do_not_replay(setup):
    conn, actions = setup
    first = actions.enqueue(incident())
    assert first['state'] == 'pending'
    assert actions.enqueue(incident()) == first
    assert StationActions(conn).enqueue(incident()) == first
    claimed = StationActions(conn).claim('kitchen', 'verified-speaker-consumer')
    assert len(claimed) == 1
    assert claimed[0]['id'] == first['id']
    assert claimed[0]['state'] == 'dispatched'
    assert claimed[0]['consumer_id'] == 'verified-speaker-consumer'
    assert StationActions(conn).claim('kitchen', 'another-consumer') == []
    assert StationActions(conn).enqueue(incident())['state'] == 'dispatched'
    assert len(actions.snapshot('kitchen')) == 1


def test_acknowledgement_is_terminal_and_idempotent(setup):
    _, actions = setup
    command = actions.enqueue(incident())
    with pytest.raises(ValueError, match='previously dispatched'):
        actions.acknowledge('kitchen', command['id'], 'delivered')
    actions.claim('kitchen', 'consumer')
    delivered = actions.acknowledge('kitchen', command['id'], 'delivered', 'One attention chime completed')
    assert delivered['state'] == 'delivered'
    assert actions.acknowledge('kitchen', command['id'], 'delivered', 'One attention chime completed') == delivered
    with pytest.raises(ValueError, match='cannot be changed'):
        actions.acknowledge('kitchen', command['id'], 'failed')
    with pytest.raises(ValueError, match='cannot be changed'):
        actions.acknowledge('kitchen', command['id'], 'delivered', 'Different result')
    assert actions.claim('kitchen', 'consumer') == []


def test_failed_delivery_is_not_automatically_retried(setup):
    conn, actions = setup
    command = actions.enqueue(incident())
    actions.claim('kitchen', 'consumer')
    failed = actions.acknowledge('kitchen', command['id'], 'failed', 'Speaker is unavailable')
    assert failed['state'] == 'failed'
    assert StationActions(conn).claim('kitchen', 'replacement') == []
    assert StationActions(conn).enqueue(incident())['state'] == 'failed'


def test_recovery_cancels_only_pending_and_keeps_audit(setup):
    _, actions = setup
    pending = actions.enqueue(incident('pending'))
    dispatched = actions.enqueue(incident('dispatched'))
    actions.cancel_for_incident('pending')
    assert [c['id'] for c in actions.claim('kitchen', 'consumer')] == [dispatched['id']]
    actions.cancel_for_incident('dispatched')
    actions.cancel_for_incident('missing-incident')
    states = {c['id']: c['state'] for c in actions.snapshot('kitchen')}
    assert states == {pending['id']: 'cancelled', dispatched['id']: 'dispatched'}
    with pytest.raises(ValueError, match='previously dispatched'):
        actions.acknowledge('kitchen', pending['id'], 'delivered')
    assert actions.enqueue(incident('pending'))['state'] == 'cancelled'


def test_board_isolation_and_conflicting_incident_binding(setup):
    _, actions = setup
    command = actions.enqueue(incident())
    assert actions.snapshot('bathroom') == []
    assert actions.claim('bathroom', 'consumer') == []
    with pytest.raises(ValueError, match='another board'):
        actions.acknowledge('bathroom', command['id'], 'delivered')
    with pytest.raises(ValueError, match='different sound command'):
        actions.enqueue(incident(board_id='bathroom'))
    with pytest.raises(ValueError, match='different sound command'):
        actions.enqueue(incident(simulation=True))
    assert actions.snapshot('kitchen')[0]['state'] == 'pending'


def test_invalid_payload_and_bounded_identifiers(setup):
    _, actions = setup
    for payload in (incident(actions=['alert']), incident(actions=None), incident(actions='sound'),
                    incident(status='recovered'), incident(simulation='false'),
                    incident(id=' '), incident(title=''), incident(title='x' * 121)):
        with pytest.raises(ValueError):
            actions.enqueue(payload)
    command = actions.enqueue(incident())
    for consumer in ('', ' ', 'x' * 129, 'consumer\nother', 1):
        with pytest.raises(ValueError):
            actions.claim('kitchen', consumer)
    actions.claim('kitchen', 'consumer')
    for status, detail in [('pending', ''), ('dispatched', ''), ('delivered', 'x' * 501), ('failed', None)]:
        with pytest.raises(ValueError):
            actions.acknowledge('kitchen', command['id'], status, detail)
    with pytest.raises(KeyError):
        actions.acknowledge('kitchen', 'missing', 'delivered')


def test_savepoints_preserve_outer_transaction(setup):
    conn, actions = setup
    conn.execute('BEGIN')
    actions.enqueue(incident())
    conn.execute('ROLLBACK')
    assert actions.snapshot('kitchen') == []
    command = actions.enqueue(incident())
    conn.execute('BEGIN')
    actions.claim('kitchen', 'consumer')
    conn.execute('ROLLBACK')
    assert actions.snapshot('kitchen')[0]['state'] == 'pending'
    assert actions.claim('kitchen', 'consumer')[0]['id'] == command['id']


def test_independent_sqlite_consumers_cannot_claim_same_command(tmp_path):
    path = str(tmp_path / 'outputs.sqlite')
    conn = db.connect(path)
    actions = StationActions(conn)
    actions.enqueue(incident())
    conn.close()

    def claim(consumer):
        independent = db.connect(path)
        try:
            return StationActions(independent).claim('kitchen', consumer)
        finally:
            independent.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ['consumer-a', 'consumer-b']))
    assert sorted(map(len, results)) == [0, 1]
