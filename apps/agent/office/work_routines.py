"""Restart-safe local schedules and follow-up inbox. This module never sends messages.

``latest`` emits at most the latest missed wall-clock slot. ``skip`` only emits a
slot on its own local calendar day. DST gaps are skipped; repeated hours use the
first fold. An unfinished occurrence can be linked rather than duplicated.
"""
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
import re
from typing import Annotated, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from office.work_models import Key, Text, WorkCreate, WorkInput
from office.work_service import WorkConflict, WorkNotFound
from office.work_templates import template_steps

Weekday = Annotated[int, Field(strict=True, ge=0, le=6)]
_CLOSED = {"completed", "cancelled", "failed"}


class RoutineCreate(WorkInput):
    event_key: Key
    name: Text
    timezone: Key
    local_time: str
    weekdays: list[Weekday] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    starts_on: date
    missed_policy: Literal["skip", "latest"] = "latest"
    overlap_policy: Literal["link", "allow"] = "link"
    work: WorkCreate
    enabled: bool = Field(default=True, strict=True)
    review_interval_hours: int = Field(default=24, strict=True, ge=1, le=168)
    max_reminders: int = Field(default=2, strict=True, ge=0, le=5)

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Timezone must be an IANA timezone") from error
        return value

    @field_validator("local_time")
    @classmethod
    def valid_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("local_time must use HH:MM")
        return value

    @field_validator("weekdays")
    @classmethod
    def unique_days(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Weekdays must be unique")
        return sorted(value)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _at(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def _iso(value):
    return value.astimezone(UTC).isoformat()


class WorkScheduler:
    def __init__(self, conn, service, clock=None):
        self.conn, self.service = conn, service
        self.clock = clock or (lambda: datetime.now(UTC))
        for statement in (
            "CREATE TABLE IF NOT EXISTS office_work_routines (id TEXT PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,request TEXT NOT NULL,body TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS office_work_occurrences (slot_key TEXT PRIMARY KEY,routine_id TEXT NOT NULL,scheduled_at TEXT NOT NULL,work_id TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS office_work_notifications (id TEXT PRIMARY KEY,body TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS office_work_followups (work_id TEXT PRIMARY KEY,body TEXT NOT NULL)",
        ):
            conn.execute(statement)

    def _now(self):
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Scheduler clock must return an aware datetime")
        return value.astimezone(UTC)

    @contextmanager
    def _transaction(self):
        # SAVEPOINT nests inside the application's transaction without committing it.
        name = "routine_" + uuid4().hex
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
        except BaseException:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
            raise

    def _slot(self, routine, day):
        if day < date.fromisoformat(routine["starts_on"]) or day.weekday() not in routine["weekdays"]:
            return None
        zone = ZoneInfo(routine["timezone"])
        wall = datetime.combine(day, time.fromisoformat(routine["local_time"]))
        local = wall.replace(tzinfo=zone, fold=0)
        candidate = local.astimezone(UTC)
        # Attaching ZoneInfo alone accepts nonexistent times. Round-trip rejects them.
        if candidate.astimezone(zone).replace(tzinfo=None) != wall:
            return None
        return candidate

    def _next_slot(self, routine, now, inclusive=False):
        local_day = now.astimezone(ZoneInfo(routine["timezone"])).date()
        day = max(local_day, date.fromisoformat(routine["starts_on"]))
        # A weekly schedule can miss one DST slot, so two weeks suffice.
        for offset in range(15):
            slot = self._slot(routine, day + timedelta(days=offset))
            if slot is not None and (slot > now or inclusive and slot == now):
                return slot
        raise WorkConflict("Cannot find next routine occurrence")

    def _latest_slot(self, routine, now):
        local_day = now.astimezone(ZoneInfo(routine["timezone"])).date()
        for offset in range(15):
            slot = self._slot(routine, local_day - timedelta(days=offset))
            if slot is not None and slot <= now:
                return slot
        return None

    def _initial_slot(self, routine, now):
        latest = self._latest_slot(routine, now)
        if latest is not None:
            local_day = now.astimezone(ZoneInfo(routine["timezone"])).date()
            if routine["missed_policy"] == "latest" or latest.astimezone(ZoneInfo(routine["timezone"])).date() == local_day:
                return latest
        return self._next_slot(routine, now, inclusive=True)

    def create(self, body: RoutineCreate):
        # Reject invalid definitions before persisting a routine that would fail
        # every future tick and stop unrelated scheduled work.
        if body.work.template_key != "custom":
            template_steps(body.work.template_key, body.work.owner)
        request = body.model_dump(mode="json")
        # Nested event identity is intentionally replaced by each occurrence identity.
        request["work"]["event_key"] = "routine-occurrence"
        payload = _json(request)
        with self._transaction():
            old = self.conn.execute("SELECT request,body FROM office_work_routines WHERE event_key=?", (body.event_key,)).fetchone()
            if old:
                if old[0] != payload:
                    raise WorkConflict("Routine event key was reused with different input")
                return json.loads(old[1])
            now = self._now()
            value = request | {"id": uuid4().hex[:12], "created_at": _iso(now), "last_occurrence_at": None}
            value["next_run_at"] = _iso(self._initial_slot(value, now)) if value["enabled"] else None
            self.conn.execute("INSERT INTO office_work_routines VALUES(?,?,?,?)", (value["id"], body.event_key, payload, _json(value)))
            return value

    def list(self):
        return [json.loads(row[0]) for row in self.conn.execute("SELECT body FROM office_work_routines ORDER BY rowid DESC")]

    def _save_routine(self, routine):
        self.conn.execute("UPDATE office_work_routines SET body=? WHERE id=?", (_json(routine), routine["id"]))

    def set_enabled(self, routine_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._transaction():
            row = self.conn.execute("SELECT body FROM office_work_routines WHERE id=?", (routine_id,)).fetchone()
            if row is None:
                raise WorkNotFound("Routine not found")
            value = json.loads(row[0])
            if value["enabled"] != enabled:
                value["enabled"] = enabled
                value["next_run_at"] = _iso(self._initial_slot(value, self._now())) if enabled else None
                self._save_routine(value)
            return value

    def notifications(self):
        return [json.loads(row[0]) for row in self.conn.execute("SELECT body FROM office_work_notifications ORDER BY rowid DESC LIMIT 500")]

    def acknowledge(self, notification_id):
        with self._transaction():
            row = self.conn.execute("SELECT body FROM office_work_notifications WHERE id=?", (notification_id,)).fetchone()
            if row is None:
                raise WorkNotFound("Notification not found")
            value = json.loads(row[0])
            if value["acknowledged_at"] is None:
                value["acknowledged_at"] = _iso(self._now())
                self.conn.execute("UPDATE office_work_notifications SET body=? WHERE id=?", (_json(value), notification_id))
            return value

    def _notify(self, key, work, kind, target, detail, now):
        value = {"id": hashlib.sha256(key.encode()).hexdigest()[:24], "work_id": work["id"], "kind": kind,
                 "target": target, "detail": detail, "created_at": _iso(now), "acknowledged_at": None, "delivery": "local_only"}
        self.conn.execute("INSERT OR IGNORE INTO office_work_notifications VALUES(?,?)", (value["id"], _json(value)))

    def _save_work(self, work, previous_version=None):
        self.conn.execute("UPDATE office_work_items SET body=? WHERE id=?", (_json(work), work["id"]))
        # Scheduler updates are not human progress, and must not reset reminder limits.
        row = self.conn.execute("SELECT body FROM office_work_followups WHERE work_id=?", (work["id"],)).fetchone()
        if row and previous_version is not None:
            followup = json.loads(row[0])
            if followup["last_version"] == previous_version:
                followup["last_version"] = work["version"]
                self.conn.execute("UPDATE office_work_followups SET body=? WHERE work_id=?", (_json(followup), work["id"]))

    def _open_occurrence(self, routine_id):
        for row in self.conn.execute("SELECT work_id FROM office_work_occurrences WHERE routine_id=? ORDER BY scheduled_at DESC", (routine_id,)):
            work = self.service.get(row[0])
            if work["status"] not in _CLOSED:
                return work
        return None

    def _run_routine(self, routine, now):
        if not routine["enabled"] or routine["next_run_at"] is None or _at(routine["next_run_at"]) > now:
            return False
        slot = self._latest_slot(routine, now)
        zone = ZoneInfo(routine["timezone"])
        should_run = slot is not None and (routine["missed_policy"] == "latest" or slot.astimezone(zone).date() == now.astimezone(zone).date())
        if should_run:
            local_date = slot.astimezone(zone).date().isoformat()
            slot_key = f"{routine['id']}:{local_date}:{routine['local_time']}:{routine['timezone']}"
            seen = self.conn.execute("SELECT 1 FROM office_work_occurrences WHERE slot_key=?", (slot_key,)).fetchone()
            if not seen:
                work = self._open_occurrence(routine["id"]) if routine["overlap_policy"] == "link" else None
                if work is None:
                    value = dict(routine["work"])
                    value["event_key"] = "routine:" + hashlib.sha256(slot_key.encode()).hexdigest()
                    value["next_review_at"] = _iso(now + timedelta(hours=routine["review_interval_hours"]))
                    # Each occurrence is due at its own slot, not a stale deadline
                    # copied from the original work template.
                    value["due_at"] = _iso(slot)
                    work = self.service.create(WorkCreate.model_validate(value))
                    work["links"]["routine_id"] = routine["id"]
                    self._save_work(work)
                else:
                    work["history"].append({"at": _iso(now), "action": "routine_overlap", "detail": f"Linked routine occurrence {local_date}; earlier outcome is still open."})
                    previous_version = work["version"]
                    work["version"] += 1
                    work["updated_at"] = _iso(now)
                    self._save_work(work, previous_version)
                    self._notify("overlap:" + slot_key, work, "escalation", work["escalation_owner"],
                                 f"Routine {routine['name']} is due again while previous work remains open.", now)
                self.conn.execute("INSERT INTO office_work_occurrences VALUES(?,?,?,?)", (slot_key, routine["id"], _iso(slot), work["id"]))
            routine["last_occurrence_at"] = _iso(slot)
        routine["next_run_at"] = _iso(self._next_slot(routine, now))
        self._save_routine(routine)
        return True

    def _review_work(self, work, now, routine_by_id):
        if work["status"] in _CLOSED or not work.get("next_review_at") or _at(work["next_review_at"]) > now:
            return False
        row = self.conn.execute("SELECT body FROM office_work_followups WHERE work_id=?", (work["id"],)).fetchone()
        state = json.loads(row[0]) if row else {"last_version": work["version"], "reminders": 0, "escalated": False, "cycle": 0}
        if state["last_version"] != work["version"]:
            state = {"last_version": work["version"], "reminders": 0, "escalated": False, "cycle": state["cycle"] + 1}
        routine = routine_by_id.get(work.get("links", {}).get("routine_id"), {})
        interval = routine.get("review_interval_hours", 24)
        limit = routine.get("max_reminders", 2)
        detail = f"Review due: {work['title']}. {work.get('waiting_reason') or work['outcome']}"
        if state["reminders"] < limit:
            state["reminders"] += 1
            self._notify(f"review:{work['id']}:{state['cycle']}:reminder:{state['reminders']}", work, "reminder", work["owner"], detail, now)
        elif not state["escalated"]:
            state["escalated"] = True
            self._notify(f"review:{work['id']}:{state['cycle']}:escalation", work, "escalation", work["escalation_owner"], detail, now)
        work["next_review_at"] = _iso(now + timedelta(hours=interval))
        work["updated_at"] = _iso(now)
        work["version"] += 1
        work["history"].append({"at": _iso(now), "action": "review_due", "detail": "Local follow-up reviewed; no external message sent."})
        self._save_work(work)
        state["last_version"] = work["version"]
        self.conn.execute("INSERT INTO office_work_followups VALUES(?,?) ON CONFLICT(work_id) DO UPDATE SET body=excluded.body", (work["id"], _json(state)))
        return True

    def tick(self):
        now = self._now()
        changed = False
        with self._transaction():
            routines = self.list()
            for routine in routines:
                changed = self._run_routine(routine, now) or changed
            by_id = {routine["id"]: routine for routine in routines}
            for work in self.service.list():
                changed = self._review_work(work, now, by_id) or changed
        return changed
