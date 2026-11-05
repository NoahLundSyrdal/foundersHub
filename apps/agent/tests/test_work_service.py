import sqlite3
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from office.work_models import EvidenceCreate, WorkAction, WorkCreate, WorkDecision
from office.work_service import WorkConflict, WorkNotFound, WorkService


@pytest.fixture
def service():
    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    return WorkService(sqlite3.connect(":memory:", isolation_level=None), clock=lambda: now)


def create(service, **overrides):
    return service.create(WorkCreate.model_validate({"event_key": "create", "title": "Restock coffee", "outcome": "Usable coffee stock is received", "template_key": "replenishment", **overrides}))


def act(service, work, action, **overrides):
    return service.act(work["id"], WorkAction.model_validate({"event_key": f"act-{work['version']}", "expected_version": work["version"], "action": action, **overrides}))


def evidence(service, work, step_id, kind):
    return service.add_evidence(work["id"], EvidenceCreate(event_key=f"proof-{work['version']}", expected_version=work["version"], step_id=step_id,
        kind=kind, summary="Verified by responsible owner", source="Local operator confirmation", recorded_by="office-owner"))


def test_idempotent_create_and_changed_payload_conflicts(service):
    one = create(service)
    assert create(service) == one
    assert len(service.list()) == 1
    with pytest.raises(WorkConflict, match="different command"):
        create(service, outcome="Something materially different")


def test_stale_updates_cannot_overwrite_owner_and_replays_return_prior_response(service):
    one = create(service)
    command = WorkAction(event_key="assign", expected_version=1, action="assign", owner="facilities-owner")
    two = service.act(one["id"], command)
    three = act(service, two, "assign", owner="another-owner")
    assert service.act(one["id"], command) == two
    assert service.get(one["id"])["owner"] == three["owner"]
    with pytest.raises(WorkConflict, match="Work changed"):
        service.act(one["id"], command.model_copy(update={"event_key": "fresh-stale"}))


def test_replenishment_cannot_close_at_order_and_requires_receipt_inventory(service):
    work = create(service)
    with pytest.raises(WorkConflict, match="All steps"):
        act(service, work, "close")
    with pytest.raises(WorkConflict, match="predecessor"):
        act(service, work, "complete_step", step_id="receipt")
    with pytest.raises(WorkConflict, match="evidence missing"):
        act(service, work, "complete_step", step_id="order")
    for step, kind in [("order", "order_confirmation"), ("receipt", "receipt"), ("inventory", "inventory_update")]:
        work = evidence(service, work, step, kind)
        work = act(service, work, "complete_step", step_id=step)
        assert work["status"] == "active"
    work = act(service, work, "close")
    assert work["status"] == "completed"
    assert work["next_review_at"] is None
    with pytest.raises(WorkConflict, match="Finished work"):
        act(service, work, "assign", owner="someone")


def test_evidence_is_bound_to_step_kind_and_work(service):
    work = create(service)
    with pytest.raises(WorkConflict, match="not required"):
        evidence(service, work, "receipt", "repair")
    work = evidence(service, work, "receipt", "receipt")
    with pytest.raises(WorkConflict, match="order_confirmation"):
        act(service, work, "complete_step", step_id="order")


def purchase(service, **overrides):
    return create(service, template_key="custom", steps=[{"id": "order", "title": "Purchase coffee", "owner": "buyer", "action_kind": "purchase",
        "action_snapshot": {"product_id": "coffee", "quantity": 2, "total_nok": 189}, "required_evidence": ["order_confirmation"]}], **overrides)


def approve(service, work, **overrides):
    return service.decide(work["id"], WorkDecision.model_validate({"event_key": f"approval-{work['version']}", "expected_version": work["version"],
        "step_id": "order", "approve": True, "proposal_fingerprint": work["steps"][0]["approval"]["fingerprint"], **overrides}))


def test_committing_actions_always_require_exact_approval(service):
    work = purchase(service)
    assert work["steps"][0]["requires_approval"] is True
    with pytest.raises(WorkConflict, match="approval is required"):
        act(service, work, "simulate_step", step_id="order")
    with pytest.raises(WorkConflict, match="changed"):
        approve(service, work, proposal_fingerprint="stale")
    work = approve(service, work)
    assert work["steps"][0]["approval"]["decided_by"] == "local-operator"
    work = act(service, work, "simulate_step", step_id="order")
    assert work["steps"][0]["execution"]["reference"].startswith("SIM-")
    with pytest.raises(WorkConflict, match="already attempted"):
        act(service, work, "simulate_step", step_id="order")
    with pytest.raises(WorkConflict, match="evidence missing"):
        act(service, work, "complete_step", step_id="order")
    work = evidence(service, work, "order", "order_confirmation")
    work = act(service, work, "complete_step", step_id="order")
    assert act(service, work, "close")["status"] == "completed"


def test_owner_change_invalidates_approval(service):
    work = approve(service, purchase(service))
    old_fingerprint = work["steps"][0]["approval"]["fingerprint"]
    work = act(service, work, "assign", step_id="order", owner="new-buyer")
    assert work["steps"][0]["approval"]["status"] == "pending"
    assert work["steps"][0]["approval"]["fingerprint"] != old_fingerprint
    with pytest.raises(WorkConflict, match="changed"):
        approve(service, work, proposal_fingerprint=old_fingerprint)


def test_uncertain_execution_must_reconcile_without_retry(service):
    work = approve(service, purchase(service))
    work = act(service, work, "simulate_step", step_id="order", result="uncertain")
    execution_id = work["steps"][0]["execution"]["id"]
    with pytest.raises(WorkConflict, match="already attempted"):
        act(service, work, "simulate_step", step_id="order")
    with pytest.raises(WorkConflict, match="Uncertain execution"):
        act(service, work, "cancel", reason="No longer needed")
    with pytest.raises(WorkConflict, match="Reconciliation requires"):
        act(service, work, "reconcile_step", step_id="order", result="confirmed")
    work = act(service, work, "reconcile_step", step_id="order", result="confirmed", reference="SIM-VERIFIED", reason="Operator checked persisted simulator record")
    assert work["steps"][0]["execution"]["id"] == execution_id
    assert work["steps"][0]["execution"]["status"] == "confirmed"


def test_real_actions_cannot_run_through_simulation(service):
    work = approve(service, purchase(service, simulation=False))
    with pytest.raises(WorkConflict, match="disabled for real-world"):
        act(service, work, "simulate_step", step_id="order")
    work = evidence(service, work, "order", "order_confirmation")
    with pytest.raises(WorkConflict, match="confirmed execution"):
        act(service, work, "complete_step", step_id="order")


def test_expired_approval_blocks_execution(service):
    work = approve(service, purchase(service))
    service.clock = lambda: datetime(2026, 11, 7, 12, tzinfo=UTC)
    with pytest.raises(WorkConflict, match="expired"):
        act(service, work, "simulate_step", step_id="order")


def test_mutations_do_not_commit_caller_transaction(service):
    service.conn.execute("BEGIN")
    work = create(service)
    assert service.conn.in_transaction
    service.conn.execute("ROLLBACK")
    with pytest.raises(WorkNotFound):
        service.get(work["id"])
    assert service.list() == []


def test_waiting_requires_review_and_retains_escalation_owner(service):
    work = create(service)
    with pytest.raises(WorkConflict, match="require a reason"):
        act(service, work, "wait", reason="Waiting for receipt")
    work = act(service, work, "wait", reason="Waiting for receipt", next_review_at=datetime(2026, 11, 6, 12, tzinfo=UTC))
    assert work["status"] == "waiting"
    assert work["escalation_owner"]
    assert work["next_review_at"]


def test_durable_work_survives_connection_restart(tmp_path):
    path = str(tmp_path / "work.sqlite")
    conn = sqlite3.connect(path, isolation_level=None)
    service = WorkService(conn)
    work = create(service)
    conn.close()
    restored = WorkService(sqlite3.connect(path, isolation_level=None))
    assert restored.get(work["id"]) == work
    assert create(restored) == work


@pytest.mark.parametrize("steps", [
    [{"id": "a", "title": "A", "owner": "owner", "depends_on": ["missing"]}],
    [{"id": "a", "title": "A", "owner": "owner", "depends_on": ["a"]}],
    [{"id": "a", "title": "A", "owner": "owner", "depends_on": ["b"]}, {"id": "b", "title": "B", "owner": "owner", "depends_on": ["a"]}],
    [{"id": "a", "title": "A", "owner": "owner"}, {"id": "a", "title": "B", "owner": "owner"}],
])
def test_invalid_dependency_graphs_rejected(steps):
    with pytest.raises(ValidationError):
        WorkCreate(event_key="event", title="Task", outcome="Outcome", steps=steps)


def test_blank_inputs_naive_dates_and_arbitrary_status_rejected():
    for bad in [{"owner": " "}, {"due_at": "2026-11-05T12:00:00"}, {"status": "completed"}]:
        with pytest.raises(ValidationError):
            WorkCreate(event_key="event", title="Task", outcome="Outcome", template_key="maintenance", **bad)


def test_completed_execution_can_collect_delivery_evidence_after_approval_expiry(service):
    work = approve(service, purchase(service))
    work = act(service, work, "simulate_step", step_id="order")
    service.clock = lambda: datetime(2026, 11, 8, 12, tzinfo=UTC)
    work = evidence(service, work, "order", "order_confirmation")
    work = act(service, work, "complete_step", step_id="order")
    assert act(service, work, "close")["status"] == "completed"
