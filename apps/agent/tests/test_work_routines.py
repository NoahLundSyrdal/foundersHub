"""Behavioral tests for local recurrence and follow-up, without model/network calls."""
from datetime import UTC, datetime, timedelta
import sqlite3

import pytest
from pydantic import ValidationError

from office.work_models import WorkAction, WorkCreate
from office.work_routines import RoutineCreate, WorkScheduler
from office.work_service import WorkConflict, WorkNotFound, WorkService


class Clock:
    def __init__(self, at="2026-11-05T08:00:00+00:00"):
        self.at = datetime.fromisoformat(at)

    def __call__(self):
        return self.at

    def set(self, at):
        self.at = datetime.fromisoformat(at)


def setup(path=":memory:", clock=None):
    clock = clock or Clock()
    conn = sqlite3.connect(path, isolation_level=None)
    service = WorkService(conn, clock)
    return conn, service, WorkScheduler(conn, service, clock), clock


def work(**updates):
    return WorkCreate.model_validate({"event_key": "work-input", "title": "Coffee ready", "outcome": "Stock received and counted",
        "owner": "office-ops", "template_key": "replenishment", **updates})


def routine(**updates):
    return RoutineCreate.model_validate({"event_key": "routine-create", "name": "Kitchen readiness", "timezone": "UTC",
        "local_time": "09:00", "starts_on": "2026-11-05", "work": work(), **updates})


def test_create_idempotency_rejects_changed_payload_and_validates_schedule():
    _, _, scheduler, _ = setup()
    original = scheduler.create(routine())
    assert scheduler.create(routine()) == original
    with pytest.raises(WorkConflict):
        scheduler.create(routine(name="Different"))
    for changes in ({"timezone": "not/a-zone"}, {"local_time": "24:00"}, {"weekdays": [1, 1]}, {"weekdays": []}, {"weekdays": [True]}, {"max_reminders": 8}):
        with pytest.raises(ValidationError):
            routine(**changes)


def test_scheduled_occurrence_survives_restart_and_repeated_tick(tmp_path):
    path = str(tmp_path / "work.sqlite")
    conn, service, scheduler, clock = setup(path)
    scheduled = scheduler.create(routine())
    assert not scheduler.tick()
    clock.set("2026-11-05T09:00:00+00:00")
    assert scheduler.tick()
    assert not scheduler.tick()
    created = service.list()[0]
    assert created["links"]["routine_id"] == scheduled["id"]
    conn.close()
    conn, service, scheduler, _ = setup(path, clock)
    assert not scheduler.tick()
    assert len(service.list()) == 1
    assert conn.execute("SELECT count(*) FROM office_work_occurrences").fetchone()[0] == 1


def test_weekday_and_timezone_schedule():
    _, service, scheduler, clock = setup(clock=Clock("2026-11-05T10:00:00+00:00"))
    scheduled = scheduler.create(routine(timezone="Europe/Oslo", weekdays=[0]))
    assert scheduled["next_run_at"] == "2026-11-09T08:00:00+00:00"
    clock.set("2026-11-09T07:59:59+00:00")
    assert not scheduler.tick()
    clock.set("2026-11-09T08:00:00+00:00")
    scheduler.tick()
    assert len(service.list()) == 1


def test_latest_policy_coalesces_long_backlog_into_latest_slot():
    _, service, scheduler, clock = setup(clock=Clock("2026-11-05T10:00:00+00:00"))
    scheduler.create(routine(starts_on="2020-01-01", overlap_policy="allow"))
    scheduler.tick()
    assert len(service.list()) == 1
    assert service.list()[0]["due_at"].startswith("2026-11-05T09:00:00")
    clock.set("2026-11-09T10:00:00+00:00")
    scheduler.tick()
    assert len(service.list()) == 2
    assert not scheduler.tick()


def test_skip_policy_does_not_backfill_past_local_days():
    _, service, scheduler, clock = setup()
    scheduler.create(routine(missed_policy="skip"))
    clock.set("2026-11-07T08:00:00+00:00")
    assert scheduler.tick()  # advances past missed slots without creating work
    assert service.list() == []
    clock.set("2026-11-07T10:00:00+00:00")
    scheduler.tick()
    assert len(service.list()) == 1
    assert service.list()[0]["due_at"].startswith("2026-11-07T09:00:00")


def test_overlap_links_open_work_and_records_local_escalation():
    conn, service, scheduler, clock = setup(clock=Clock("2026-11-05T10:00:00+00:00"))
    scheduler.create(routine(overlap_policy="link"))
    scheduler.tick()
    first = service.list()[0]
    clock.set("2026-11-06T10:00:00+00:00")
    scheduler.tick()
    assert len(service.list()) == 1
    assert conn.execute("SELECT count(*) FROM office_work_occurrences").fetchone()[0] == 2
    assert service.get(first["id"])["history"][-1]["action"] in {"routine_overlap", "review_due"}
    escalations = [n for n in scheduler.notifications() if n["kind"] == "escalation"]
    assert len(escalations) == 1 and escalations[0]["delivery"] == "local_only"
    assert not scheduler.tick()


def test_overlap_allow_creates_independent_obligations():
    _, service, scheduler, clock = setup(clock=Clock("2026-11-05T10:00:00+00:00"))
    scheduler.create(routine(overlap_policy="allow"))
    scheduler.tick()
    clock.set("2026-11-06T10:00:00+00:00")
    scheduler.tick()
    assert len(service.list()) == 2
    assert len({item["event_key"] for item in service.list()}) == 2


def test_dst_gap_is_skipped_and_ambiguous_hour_occurs_once():
    _, service, scheduler, clock = setup(clock=Clock("2026-03-28T12:00:00+00:00"))
    scheduled = scheduler.create(routine(timezone="Europe/Oslo", local_time="02:30", weekdays=[6], starts_on="2026-03-29"))
    assert scheduled["next_run_at"] == "2026-04-05T00:30:00+00:00"
    clock.set("2026-03-29T03:00:00+00:00")
    assert not scheduler.tick()
    assert not service.list()

    _, service, scheduler, clock = setup(clock=Clock("2026-10-25T00:31:00+00:00"))
    scheduler.create(routine(timezone="Europe/Oslo", local_time="02:30", starts_on="2026-10-25"))
    scheduler.tick()
    assert len(service.list()) == 1
    clock.set("2026-10-25T01:31:00+00:00")
    assert not scheduler.tick()
    assert len(service.list()) == 1


def test_followup_is_bounded_persistent_and_preserves_waiting_status(tmp_path):
    path = str(tmp_path / "work.sqlite")
    conn, service, scheduler, clock = setup(path)
    item = service.create(work(next_review_at=clock()))
    item = service.act(item["id"], WorkAction(event_key="wait", expected_version=item["version"], action="wait",
                                             reason="Waiting for receipt", next_review_at=clock() + timedelta(seconds=1)))
    clock.at += timedelta(seconds=1)
    scheduler.tick()
    assert len(scheduler.notifications()) == 1
    assert not scheduler.tick()
    for _ in range(4):
        clock.at += timedelta(days=1)
        scheduler.tick()
    notes = scheduler.notifications()
    assert len(notes) == 3
    assert [n["kind"] for n in reversed(notes)] == ["reminder", "reminder", "escalation"]
    assert notes[0]["target"] == "office-coordinator"
    current = service.get(item["id"])
    assert current["status"] == "waiting" and current["waiting_reason"] == "Waiting for receipt"
    assert datetime.fromisoformat(current["next_review_at"]) > clock()
    conn.close()
    _, service, scheduler, _ = setup(path, clock)
    assert not scheduler.tick()
    assert len(scheduler.notifications()) == 3
    notification_id = notes[0]["id"]
    acknowledged = scheduler.acknowledge(notification_id)
    assert acknowledged["acknowledged_at"] is not None
    assert scheduler.acknowledge(notification_id) == acknowledged
    with pytest.raises(WorkNotFound):
        scheduler.acknowledge("missing")
    clock.at += timedelta(days=1)
    current = service.get(item["id"])
    service.act(item["id"], WorkAction(event_key="new-owner", expected_version=current["version"], action="assign", owner="new-ops"))
    scheduler.tick()
    assert len(scheduler.notifications()) == 4
    assert scheduler.notifications()[0]["target"] == "new-ops"


def test_routine_limits_override_default_followup_policy():
    _, _, scheduler, clock = setup(clock=Clock("2026-11-05T10:00:00+00:00"))
    scheduler.create(routine(max_reminders=0, review_interval_hours=1))
    scheduler.tick()
    clock.at += timedelta(hours=1)
    scheduler.tick()
    assert [n["kind"] for n in scheduler.notifications()] == ["escalation"]
    for _ in range(3):
        clock.at += timedelta(hours=1)
        scheduler.tick()
    assert len(scheduler.notifications()) == 1


def test_disable_and_reenable_are_explicit_and_idempotent():
    _, service, scheduler, clock = setup()
    item = scheduler.create(routine())
    assert scheduler.set_enabled(item["id"], False)["next_run_at"] is None
    clock.set("2026-11-05T10:00:00+00:00")
    assert not scheduler.tick()
    assert not service.list()
    enabled = scheduler.set_enabled(item["id"], True)
    assert scheduler.set_enabled(item["id"], True) == enabled
    scheduler.tick()
    assert len(service.list()) == 1
    with pytest.raises(WorkNotFound):
        scheduler.set_enabled("missing", False)


def test_scheduler_does_not_commit_callers_transaction():
    conn, service, scheduler, clock = setup(clock=Clock("2026-11-05T10:00:00+00:00"))
    conn.execute("BEGIN")
    scheduler.create(routine())
    scheduler.tick()
    assert len(service.list()) == 1
    conn.execute("ROLLBACK")
    assert not service.list() and not scheduler.list()
    assert not scheduler.notifications()


def test_human_progress_resets_limits_even_when_next_occurrence_overlaps():
    _, service, scheduler, clock = setup(clock=Clock("2026-11-05T10:00:00+00:00"))
    scheduler.create(routine(max_reminders=1, review_interval_hours=1))
    scheduler.tick()
    clock.at += timedelta(hours=1)
    scheduler.tick()
    clock.at += timedelta(hours=1)
    scheduler.tick()
    assert len([n for n in scheduler.notifications() if n["kind"] == "reminder"]) == 1
    item = service.list()[0]
    service.act(item["id"], WorkAction(event_key="progress-before-overlap", expected_version=item["version"], action="assign", owner="new-owner"))
    clock.set("2026-11-06T10:00:00+00:00")
    scheduler.tick()
    reminders = [n for n in scheduler.notifications() if n["kind"] == "reminder"]
    assert len(reminders) == 2 and reminders[0]["target"] == "new-owner"
