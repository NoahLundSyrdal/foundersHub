"""Durable local work coordination, exact approvals and evidence-gated closure.

This layer never calls a supplier or messaging service. Existing domain executors
remain authoritative; simulated step execution is explicitly labelled.
"""
import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from office.work_models import EvidenceCreate, StepCreate, WorkAction, WorkCreate, WorkDecision
from office.work_templates import template_steps

TERMINAL = {"completed", "cancelled", "failed"}
COMMITTING_ACTIONS = {"purchase", "message", "booking", "access"}


class WorkConflict(ValueError):
    pass


class WorkNotFound(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds")


class WorkService:
    def __init__(self, conn, clock=None):
        self.conn = conn
        self.clock = clock or (lambda: datetime.now(UTC))
        conn.execute("CREATE TABLE IF NOT EXISTS office_work_items(id TEXT PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,body TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS office_work_commands(event_key TEXT PRIMARY KEY,request TEXT NOT NULL,response TEXT NOT NULL)")

    def _now(self):
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Work clock must return an aware datetime")
        return value.astimezone(UTC)

    @contextmanager
    def _atomic(self):
        # SAVEPOINT works in both autocommit and a caller-owned transaction.
        key = "work_" + uuid.uuid4().hex
        self.conn.execute(f"SAVEPOINT {key}")
        try:
            yield
            self.conn.execute(f"RELEASE SAVEPOINT {key}")
        except BaseException:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {key}")
            self.conn.execute(f"RELEASE SAVEPOINT {key}")
            raise

    def _command(self, scope, body, callback):
        request = canonical({"scope": scope, "body": body.model_dump(mode="json")})
        with self._atomic():
            prior = self.conn.execute("SELECT request,response FROM office_work_commands WHERE event_key=?", (body.event_key,)).fetchone()
            if prior:
                if prior[0] != request:
                    raise WorkConflict("Event key was already used with different command content")
                return json.loads(prior[1])
            result = callback()
            self.conn.execute("INSERT INTO office_work_commands VALUES(?,?,?)", (body.event_key, request, canonical(result)))
            return result

    def _save(self, work):
        self.conn.execute("UPDATE office_work_items SET body=? WHERE id=?", (canonical(work), work["id"]))

    def get(self, id):
        row = self.conn.execute("SELECT body FROM office_work_items WHERE id=?", (id,)).fetchone()
        if not row:
            raise WorkNotFound("Work item not found")
        return json.loads(row[0])

    def list(self):
        return [json.loads(row[0]) for row in self.conn.execute("SELECT body FROM office_work_items ORDER BY rowid DESC")]

    def _fingerprint(self, work, step):
        # Identity, performer and required evidence are part of the reviewed scope.
        return hashlib.sha256(canonical({"work_id": work["id"], "step_id": step["id"],
            "owner": step["owner"], "action_kind": step["action_kind"], "action_snapshot": step["action_snapshot"],
            "depends_on": step["depends_on"], "required_evidence": step["required_evidence"]}).encode()).hexdigest()

    def create(self, body: WorkCreate):
        def apply():
            now = self._now()
            data = body.model_dump(mode="json")
            raw_steps = body.steps
            if raw_steps is None:
                try:
                    raw_steps = [StepCreate.model_validate(s) for s in template_steps(body.template_key, body.owner)]
                except ValueError as error:
                    raise WorkConflict(str(error)) from error
            elif body.template_key != "custom":
                # Validate the template name even when callers override the checklist.
                try:
                    template_steps(body.template_key, body.owner)
                except ValueError as error:
                    raise WorkConflict(str(error)) from error
            next_review = body.next_review_at or min(body.due_at or now + timedelta(days=1), now + timedelta(days=1))
            work = {**data, "id": uuid.uuid4().hex[:16], "status": "ready", "next_review_at": stamp(next_review),
                    "waiting_reason": None, "version": 1, "created_at": stamp(now), "updated_at": stamp(now),
                    "steps": [], "evidence": [], "history": [{"at": stamp(now), "action": "created", "detail": "Work created"}], "links": {}}
            for raw in raw_steps:
                step = raw.model_dump(mode="json")
                requires_approval = step["requires_approval"] or step["action_kind"] in COMMITTING_ACTIONS
                step |= {"requires_approval": requires_approval, "status": "pending", "execution": None, "approval": None}
                if requires_approval:
                    step["approval"] = {"fingerprint": self._fingerprint(work, step), "status": "pending",
                                        "expires_at": stamp(now + timedelta(days=1)), "decided_by": None}
                work["steps"].append(step)
            self.conn.execute("INSERT INTO office_work_items VALUES(?,?,?)", (work["id"], work["event_key"], canonical(work)))
            return work
        return self._command("create", body, apply)

    def _mutable(self, id, version):
        work = self.get(id)
        if work["version"] != version:
            raise WorkConflict("Work changed; refresh and review the current version")
        if work["status"] in TERMINAL:
            raise WorkConflict("Finished work cannot be changed")
        return work

    def _step(self, work, id):
        step = next((step for step in work["steps"] if step["id"] == id), None)
        if step is None:
            raise WorkConflict("Step does not exist in this work item")
        return step

    def _dependencies(self, work, step):
        if any(self._step(work, key)["status"] != "completed" for key in step["depends_on"]):
            raise WorkConflict("Required predecessor steps are not completed")

    def _approval(self, work, step):
        if not step["requires_approval"]:
            return
        approval = step["approval"]
        if not approval or approval["status"] != "approved":
            raise WorkConflict("Exact action approval is required")
        if approval["fingerprint"] != self._fingerprint(work, step):
            raise WorkConflict("Approved action changed; review it again")
        if (not step["execution"] or step["execution"]["status"] != "confirmed") and datetime.fromisoformat(approval["expires_at"]) <= self._now():
            raise WorkConflict("Action approval expired; use Renew approval, then review the exact action again")

    def _evidence(self, work, step):
        kinds = {item["kind"] for item in work["evidence"] if item["step_id"] == step["id"]}
        missing = set(step["required_evidence"]) - kinds
        if missing:
            raise WorkConflict("Completion evidence missing: " + ", ".join(sorted(missing)))

    def _finish_mutation(self, work, action, detail):
        now = stamp(self._now())
        work["version"] += 1
        work["updated_at"] = now
        work["history"].append({"at": now, "action": action, "detail": detail})
        self._save(work)
        return work

    def add_evidence(self, id, body: EvidenceCreate):
        def apply():
            work = self._mutable(id, body.expected_version)
            step = self._step(work, body.step_id)
            if step["status"] == "completed":
                raise WorkConflict("Completed step evidence is immutable")
            if body.kind not in step["required_evidence"]:
                raise WorkConflict("Evidence kind is not required for this step")
            work["evidence"].append({"id": uuid.uuid4().hex[:16], "step_id": body.step_id, "kind": body.kind,
                "summary": body.summary, "source": body.source, "recorded_by": body.recorded_by,
                "created_at": stamp(self._now()), "simulation": work["simulation"]})
            return self._finish_mutation(work, "evidence", f"Recorded {body.kind} evidence for {step['title']}")
        return self._command(f"evidence:{id}", body, apply)

    def decide(self, id, body: WorkDecision):
        def apply():
            work = self._mutable(id, body.expected_version)
            step = self._step(work, body.step_id)
            if step["status"] == "completed" or step["execution"]:
                raise WorkConflict("Execution has already started or this step is completed")
            approval = step["approval"]
            if not approval or not step["requires_approval"]:
                raise WorkConflict("Step does not require approval")
            if approval["status"] != "pending":
                raise WorkConflict("Action already decided; its decision cannot be replaced")
            self._dependencies(work, step)
            if body.proposal_fingerprint != approval["fingerprint"] or body.proposal_fingerprint != self._fingerprint(work, step):
                raise WorkConflict("Proposed action changed; review it again")
            if datetime.fromisoformat(approval["expires_at"]) <= self._now():
                raise WorkConflict("Proposed action expired; use Renew approval, then review the exact action again")
            approval["status"] = "approved" if body.approve else "rejected"
            approval["decided_by"] = "local-operator"
            step["status"] = "pending" if body.approve else "blocked"
            work["status"] = "ready" if body.approve else "blocked"
            work["waiting_reason"] = None if body.approve else "Action approval rejected"
            return self._finish_mutation(work, "approval", f"Action {'approved' if body.approve else 'rejected'} by local operator")
        return self._command(f"decision:{id}", body, apply)

    def act(self, id, body: WorkAction):
        def apply():
            work = self._mutable(id, body.expected_version)
            action = body.action
            if action == "assign":
                if not body.owner:
                    raise WorkConflict("An accountable owner is required")
                if body.step_id:
                    step = self._step(work, body.step_id)
                    if step["execution"] or step["status"] == "completed":
                        raise WorkConflict("Executed or completed step ownership cannot change")
                    step["owner"] = body.owner
                    if step["approval"]:
                        step["approval"] = {"fingerprint": self._fingerprint(work, step), "status": "pending",
                                            "expires_at": stamp(self._now() + timedelta(days=1)), "decided_by": None}
                        step["status"] = "awaiting_approval"
                else:
                    work["owner"] = body.owner
            elif action in {"wait", "block"}:
                if not body.reason or body.next_review_at is None:
                    raise WorkConflict("Waiting and blocked work require a reason and next review")
                if body.next_review_at <= self._now():
                    raise WorkConflict("Next review must be in the future")
                work["status"] = "waiting" if action == "wait" else "blocked"
                work["waiting_reason"] = body.reason
                work["next_review_at"] = stamp(body.next_review_at)
            elif action == "resume":
                work["status"], work["waiting_reason"] = "active", None
                if body.next_review_at:
                    work["next_review_at"] = stamp(body.next_review_at)
            elif action in {"cancel", "fail"}:
                if not body.reason:
                    raise WorkConflict("A terminal reason is required")
                if any(s["execution"] and s["execution"]["status"] in {"started", "uncertain"} for s in work["steps"]):
                    raise WorkConflict("Uncertain execution must be reconciled before ending work")
                work["status"] = "cancelled" if action == "cancel" else "failed"
                work["waiting_reason"] = body.reason
                work["next_review_at"] = None
            elif action == "close":
                if any(s["status"] != "completed" for s in work["steps"]):
                    raise WorkConflict("All steps must be completed before closing work")
                for step in work["steps"]:
                    self._evidence(work, step)
                    if step["execution"] and step["execution"]["status"] != "confirmed":
                        raise WorkConflict("Unconfirmed execution prevents closure")
                work["status"], work["waiting_reason"], work["next_review_at"] = "completed", None, None
            else:
                step = self._step(work, body.step_id)
                if step["status"] == "completed":
                    raise WorkConflict("Step is already completed")
                self._dependencies(work, step)
                if action == "renew_approval":
                    approval = step["approval"]
                    if step["execution"] or not approval or approval["status"] == "rejected":
                        raise WorkConflict("Only unexecuted, non-rejected approvals can be renewed")
                    if datetime.fromisoformat(approval["expires_at"]) > self._now():
                        raise WorkConflict("Approval has not expired")
                    step["approval"] = {"fingerprint": self._fingerprint(work, step), "status": "pending",
                        "expires_at": stamp(self._now() + timedelta(days=1)), "decided_by": None}
                    step["status"] = "awaiting_approval"
                    work["status"], work["waiting_reason"] = "awaiting_approval", "Review renewed exact action"
                elif action == "start_step":
                    if step["execution"]:
                        raise WorkConflict("An execution attempt already exists; reconcile it instead")
                    self._approval(work, step)
                    step["status"] = "active"
                    work["status"], work["waiting_reason"] = "active", None
                elif action == "simulate_step":
                    if not work["simulation"]:
                        raise WorkConflict("Simulation execution is disabled for real-world work")
                    if step["execution"]:
                        raise WorkConflict("Execution was already attempted; reconcile its persisted result")
                    self._approval(work, step)
                    result = body.result or "confirmed"
                    step["execution"] = {"id": uuid.uuid4().hex[:16], "status": result,
                                         "reference": "SIM-" + uuid.uuid4().hex[:8].upper()}
                    step["status"] = "active" if result == "confirmed" else result
                    work["status"] = "active" if result == "confirmed" else "blocked"
                    work["waiting_reason"] = None if result == "confirmed" else f"Simulated execution {result}; owner review required"
                elif action == "reconcile_step":
                    execution = step["execution"]
                    if not execution or execution["status"] not in {"started", "uncertain"}:
                        raise WorkConflict("Only an uncertain or started execution can be reconciled")
                    if body.result not in {"confirmed", "failed"} or not body.reference or not body.reason:
                        raise WorkConflict("Reconciliation requires confirmed/failed result, reference and evidence reason")
                    execution["status"], execution["reference"] = body.result, body.reference
                    step["status"] = "active" if body.result == "confirmed" else "failed"
                    work["status"] = "active" if body.result == "confirmed" else "blocked"
                    work["waiting_reason"] = None if body.result == "confirmed" else body.reason
                elif action == "complete_step":
                    self._approval(work, step)
                    self._evidence(work, step)
                    if step["action_kind"] in COMMITTING_ACTIONS and (not step["execution"] or step["execution"]["status"] != "confirmed"):
                        raise WorkConflict("Committed actions require confirmed execution")
                    if step["execution"] and step["execution"]["status"] != "confirmed":
                        raise WorkConflict("Execution is not confirmed")
                    step["status"] = "completed"
                    work["status"], work["waiting_reason"] = "active", None
            detail = body.reason or f"{action.replace('_', ' ').capitalize()}" + (f": {body.step_id}" if body.step_id else "")
            return self._finish_mutation(work, action, detail)
        return self._command(f"action:{id}", body, apply)
