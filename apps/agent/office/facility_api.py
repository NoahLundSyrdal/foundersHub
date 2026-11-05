"""Deterministic sensor routing and local actions, using the existing purchase policy."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from office import bus, config, db, procurement_workflow as workflow
from office.deps import Deps
from office.facility_rules import FacilityEngine, FacilityReading, FacilityRule
from office.facility_outputs import StationActions
from office_procurement.planning import needs_approval, plan_purchase

ALIASES = {
    "co2": "co2_ppm", "co2_ppm": "co2_ppm", "temperature": "temperature_c",
    "th_temperature": "temperature_c", "temperature_c": "temperature_c",
    "humidity": "humidity_pct", "th_humidity": "humidity_pct", "humidity_pct": "humidity_pct",
    "co2_co2": "co2_ppm", "co2_temperature": "temperature_c", "co2_humidity": "humidity_pct",
    "aht20_temperature": "temperature_c", "aht20_humidity": "humidity_pct",
    **{key: key for key in ("stock_level", "bin_fill_pct", "obstruction_mm", "noise_level", "tilt_deg", "heartbeat")},
}


def metric_for(key: str) -> str | None:
    return ALIASES.get(key.lower())


def _id(incident_id: str, action: str) -> str:
    return hashlib.sha256(f"facility:{incident_id}:{action}".encode()).hexdigest()[:24]


def _task_update(conn, task_id, status, reply, pending=None):
    conn.execute("UPDATE tasks SET status=?,reply=?,pending=?,updated_at=? WHERE id=?",
                 (status, reply, json.dumps(pending or []), db.now(), task_id))
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    value = workflow.get_context(conn, task_id)
    if value and value.get("source") == "facility-rule":
        stored = db.row(conn, "SELECT body FROM facility_incidents WHERE id=?", value["facility_incident_id"])
        if stored:
            incident = json.loads(stored["body"])
            incident["action_results"]["replenish"] = {"status": status, "task_id": task_id, "simulation": True}
            conn.execute("UPDATE facility_incidents SET body=? WHERE id=?", (json.dumps(incident), incident["id"]))
            bus.publish("facility.updated", {}, source=conn)
    db.set_command(conn, task["board_id"], reply, "red" if status == "failed" else "amber" if status == "awaiting_approval" else "green")
    bus.publish("requests.updated", {"request_id": task_id}, source=conn)
    bus.publish("state.changed", {}, source=conn)
    return workflow.enrich_task(conn, db.task_view(db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)))


async def _execute_replenishment(conn, task_id, approved=False):
    # Reuse the sole supplier boundary, including persisted attempts, limits and quote checks.
    from office.plugins.procurement import place_order
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    value = workflow.get_context(conn, task_id)
    pending = json.loads(task["pending"])
    call_id = pending[0]["tool_call_id"] if pending else f"facility-order-{task_id}"
    ctx = SimpleNamespace(deps=Deps(config.BOARDS[task["board_id"]], config.GUEST, task_id, conn),
                          tool_call_id=call_id, tool_call_approved=approved)
    proposal = value["proposal"]
    reply = await place_order(ctx, proposal["product"]["id"], proposal["quantity"])
    order = db.row(conn, "SELECT id FROM orders WHERE task_id=?", task_id)
    return _task_update(conn, task_id, "done" if order else "failed", reply)


async def decide_replenishment(conn, task_id, approve, fingerprint=None):
    """Resume a deterministic proposal without calling an interpreter or chat model."""
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    if not task or task["status"] != "awaiting_approval":
        raise workflow.ProcurementConflict("Task is not awaiting approval")
    if not approve:
        return _task_update(conn, task_id, "done", "Replenishment declined; no order was placed.")
    workflow.authorize_pending(conn, task_id, json.loads(task["pending"]), fingerprint)
    return await _execute_replenishment(conn, task_id, approved=True)


class Telemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    key: str = Field(min_length=1, max_length=64)
    value: float
    board_id: str = "kitchen"
    event_id: str | None = Field(default=None, min_length=1, max_length=128)
    timestamp: str | None = None
    simulation: bool = False


class ActionClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    consumer_id: str = Field(min_length=1, max_length=128)


class ActionAcknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["delivered", "failed"]
    detail: str = Field(default="", max_length=500)


class FacilityService:
    def __init__(self, conn, lock, rules_path=None, clock=None):
        self.conn, self.lock = conn, lock
        self.engine = FacilityEngine(conn, rules_path=rules_path, clock=clock)
        self.outputs = StationActions(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS facility_device_inputs(id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    def validate_device_retry(self, event_id, body):
        saved = db.row(self.conn, "SELECT body FROM facility_device_inputs WHERE id=?", event_id)
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if saved and saved["body"] != encoded:
            raise ValueError("Event id was already used for a different device check")

    def remember_device_input(self, event_id, body):
        self.validate_device_retry(event_id, body)
        self.conn.execute("INSERT OR IGNORE INTO facility_device_inputs VALUES(?,?)",
                          (event_id, json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)))

    def validate_stock_item(self, board_id, item_id):
        stations, products, _, _ = workflow.current_configuration()
        station = next((s for s in stations if s["id"] == board_id), None)
        product = next((p for p in products if p["id"] == item_id and p["station_id"] == board_id), None)
        if not station or not product or item_id not in station["item_ids"] or product.get("fulfillment_kind", "goods") != "goods":
            raise ValueError("Stock check must reference an approved supply for this station")
        return product

    async def prepare_stock_check(self, task_id, board_id, item_id, simulation=True):
        """A normalized stock_low edge already represents a completed deterministic check."""
        product = self.validate_stock_item(board_id, item_id)
        if type(simulation) is not bool:
            raise ValueError("simulation must be a boolean")
        rule = FacilityRule.model_validate({
            "id": "stock-check", "title": "Supply running low", "board_id": board_id,
            "metric": "stock_level", "enabled": True, "direction": "below",
            "trigger_value": 20.0, "reset_value": 30.0, "min_samples": 1,
            "min_duration_seconds": 0.0, "max_gap_seconds": 30.0,
            "actions": ["replenish"], "severity": "routine", "item_id": item_id,
            "summary": f"Stock check: replenish one catalogue unit of {product['name']}.",
        })
        incident = {"id": f"device-{task_id}", "board_id": board_id, "summary": rule.summary,
                    "severity": "routine", "simulation": simulation}
        await self._replenish(incident, rule, task_id=task_id)
        return workflow.enrich_task(self.conn, db.task_view(db.row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)))

    async def resume_stock_checks(self):
        # Registration is persisted before dispatch. Resume interrupted preparation,
        # while uncertain supplier execution remains a human reconciliation task.
        pending = db.rows(self.conn, """SELECT t.id,f.body FROM tasks t
            JOIN device_events d ON d.task_id=t.id JOIN facility_device_inputs f ON f.id=d.event_id
            WHERE t.status='running' AND json_extract(f.body,'$.type')='stock_low'""")
        for task in pending:
            attempt = db.row(self.conn, "SELECT state FROM procurement_attempts WHERE task_id=?", task["id"])
            if attempt and attempt["state"] in {"started", "uncertain"}:
                _task_update(self.conn, task["id"], "failed", "Execution outcome uncertain; reconcile the persisted attempt before any retry.")
                continue
            event = json.loads(task["body"])
            try:
                await self.prepare_stock_check(task["id"], event["station_id"], event["payload"]["item_id"], event["payload"].get("simulation", True))
            except (ValueError, KeyError, workflow.ProcurementConflict):
                _task_update(self.conn, task["id"], "failed", "Stored stock check needs administrator review; no automatic retry.")

    def validate_rule(self, rule):
        if rule.board_id not in config.BOARDS:
            raise ValueError("Rule references an unknown board")
        if "replenish" in rule.actions:
            stations, products, _, _ = workflow.current_configuration()
            station = next((s for s in stations if s["id"] == rule.board_id), None)
            product = next((p for p in products if p["id"] == rule.item_id and p["station_id"] == rule.board_id), None)
            if not station or not product or rule.item_id not in station["item_ids"] or product.get("fulfillment_kind", "goods") != "goods":
                raise ValueError("Replenishment must reference an approved supply for this board")

    def owns_metric(self, board_id, metric):
        # Disabled facility rules still own their metric: disabling cannot expose a legacy trigger.
        return any(r["board_id"] == board_id and r["metric"] == metric for r in self.engine.snapshot()["rules"])

    def _ticket(self, incident):
        task_id, ticket_id = _id(incident["id"], "ticket-task"), _id(incident["id"], "ticket")
        team = config.BOARDS[incident["board_id"]].responsible_team
        label = "Simulated input: " if incident["simulation"] else ""
        summary = label + incident["summary"]
        self.conn.execute("INSERT OR IGNORE INTO tasks(id,board_id,person,prompt,status,reply,created_at,updated_at) VALUES(?,?,?,?,'done',?,?,?)",
                          (task_id, incident["board_id"], "sensor", summary, "Local inspection task created; nobody has been contacted.", db.now(), db.now()))
        self.conn.execute("INSERT OR IGNORE INTO tickets VALUES(?,?,?,?,?,'open',?)",
                          (ticket_id, task_id, incident["board_id"], summary, team, db.now()))
        return {"status": "created", "task_id": task_id, "ticket_id": ticket_id, "team": team, "local_only": True}

    async def _replenish(self, incident, rule, task_id=None):
        self.validate_rule(rule)
        task_id = task_id or _id(incident["id"], "replenish")
        task = db.row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        value = workflow.get_context(self.conn, task_id)
        if not task:
            self.conn.execute("INSERT INTO tasks(id,board_id,person,prompt,status,created_at,updated_at) VALUES(?,?,?,?,'running',?,?)",
                              (task_id, rule.board_id, "sensor", incident["summary"], db.now(), db.now()))
        if value is None:
            stations, products, templates, policy = workflow.current_configuration()
            station = next(s for s in stations if s["id"] == rule.board_id)
            assessment = {"severity": incident["severity"], "confidence": 1.0,
                          "requires_confirmation": incident["simulation"] or incident["severity"] == "critical",
                          "reason": "Fixed configured stock rule matched; purchasing remains subject to office policy."}
            plan = plan_purchase(station, rule.item_id, 1, workflow.open_requests(self.conn, task_id), products, templates, policy, time.time())
            value = {"source": "facility-rule", "facility_incident_id": incident["id"],
                     "interpretation": {"intent": "purchase", "item_id": rule.item_id, "summary": incident["summary"],
                                        "quantity": 1, "question": None, "assessment": assessment},
                     "assessment": assessment, "research": [], "proposal": plan.get("proposal"),
                     "approval_required": True, "prepared_at": db.now()}
            if plan["kind"] == "duplicate":
                value["duplicate_task_id"] = plan["request"]["id"]
            elif plan["kind"] == "clarification":
                value["question"] = plan["question"]
            elif plan["kind"] == "proposal":
                value["approval_required"] = needs_approval(value, policy)
                value["approval"] = workflow.approval_route(value["proposal"], policy)
            workflow.store_context(self.conn, task_id, value)
        if value.get("duplicate_task_id"):
            _task_update(self.conn, task_id, "done", f"Equivalent replenishment already active ({value['duplicate_task_id']}); no additional order.")
            return {"status": "duplicate", "task_id": value["duplicate_task_id"]}
        if not value.get("proposal"):
            _task_update(self.conn, task_id, "done", value.get("question", "No approved offer is available."))
            return {"status": "needs_review", "task_id": task_id}
        task = db.row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
        if task["status"] == "running":
            pending = [{"tool_call_id": f"facility-order-{task_id}", "tool": "place_order",
                        "args": {"product_id": rule.item_id, "quantity": 1},
                        "details": workflow.approval_metadata(value, "sensor")}]
            if value["approval_required"]:
                _task_update(self.conn, task_id, "awaiting_approval", "Replenishment proposal ready for exact approval (simulated supplier).", pending)
            else:
                await _execute_replenishment(self.conn, task_id)
        task = db.row(self.conn, "SELECT status FROM tasks WHERE id=?", task_id)
        return {"status": task["status"], "task_id": task_id, "simulation": True}

    async def dispatch(self):
        snapshot = self.engine.snapshot()
        rules = {r["id"]: FacilityRule.model_validate(r) for r in snapshot["rules"]}
        changed = False
        for incident in snapshot["incidents"]:
            if incident["status"] != "active":
                self.outputs.cancel_for_incident(incident["id"])
                sound = incident["action_results"].get("sound", {})
                if sound.get("action_id"):
                    output = next((o for o in self.outputs.snapshot(incident["board_id"]) if o["id"] == sound["action_id"]), None)
                    if output and sound.get("status") != output["state"]:
                        self.engine.update_action_results(incident["id"], {"sound": {**sound, "status": output["state"]}})
                        changed = True
                continue
            results = incident["action_results"]
            incident_changed = False
            for action in incident["actions"]:
                if action in results:
                    continue
                try:
                    if action == "ticket":
                        result = self._ticket(incident)
                    elif action == "replenish":
                        result = await self._replenish(incident, rules[incident["rule_id"]])
                    elif action == "sound":
                        command = self.outputs.enqueue(incident)
                        result = {"status": command["state"], "action_id": command["id"], "output": "station"}
                    else:
                        result = {"status": "visible", "output": "dashboard"}
                    results[action] = result
                except (ValueError, KeyError, workflow.ProcurementConflict) as error:
                    # Persist a visible failure; never blindly retry an uncertain purchase.
                    results[action] = {"status": "failed", "detail": str(error)}
                self.engine.update_action_results(incident["id"], {action: results[action]})
                changed = True
                incident_changed = True
            if results:
                color = "red" if incident["severity"] == "critical" else "amber"
                if incident_changed:
                    db.set_command(self.conn, incident["board_id"], incident["title"], color)
        if changed:
            bus.publish("state.changed", {}, source=self.conn)
            bus.publish("requests.updated", {}, source=self.conn)
        return changed

    async def tick(self):
        await self.resume_stock_checks()
        changes = self.engine.tick()
        changed = await self.dispatch()
        if changes or changed:
            bus.publish("facility.updated", {}, source=self.conn)
        return changes

    async def ingest(self, reading):
        if reading.board_id not in config.BOARDS:
            raise ValueError("Unknown board")
        result = self.engine.ingest(reading)
        await self.dispatch()
        active_boards = {i["board_id"] for i in self.engine.snapshot()["incidents"] if i["status"] == "active"}
        for incident in result["incidents"]:
            if incident["status"] == "recovered" and incident["board_id"] not in active_boards:
                db.set_command(self.conn, incident["board_id"], f"Recovered: {incident['title']}", "green")
        # Only physical canonical readings enter the agent's sensor tools.
        if not reading.simulation and not result["duplicate"]:
            self.conn.execute("INSERT INTO sensor_readings VALUES(?,?,?,?)",
                              (reading.board_id, reading.metric, reading.value, reading.timestamp))
        bus.publish("facility.updated", {}, source=self.conn)
        snapshot = self.engine.snapshot()
        current = {i["id"]: i for i in snapshot["incidents"]}
        return {**result, "incidents": [current[i["id"]] for i in result["incidents"]]}

    async def watch(self):
        while True:
            await asyncio.sleep(2)
            async with self.lock:
                await self.tick()


def router(service):
    routes = APIRouter()

    def board_or_404(board_id):
        if board_id not in config.BOARDS:
            raise HTTPException(404, "Unknown board")

    @routes.get("/api/devices/{board_id}/actions")
    async def actions(board_id: str):
        board_or_404(board_id)
        async with service.lock:
            return {"actions": service.outputs.snapshot(board_id)}

    @routes.post("/api/devices/{board_id}/actions/claim")
    async def claim(board_id: str, body: ActionClaim):
        board_or_404(board_id)
        async with service.lock:
            try:
                commands = service.outputs.claim(board_id, body.consumer_id)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            for command in commands:
                service.engine.update_action_results(command["incident_id"], {"sound": {
                    "status": command["state"], "action_id": command["id"], "output": "station"}})
            bus.publish("facility.updated", {}, source=service.conn)
            return {"actions": commands}

    @routes.post("/api/devices/{board_id}/actions/{action_id}/acknowledge")
    async def acknowledge_action(board_id: str, action_id: str, body: ActionAcknowledgement):
        board_or_404(board_id)
        async with service.lock:
            try:
                command = service.outputs.acknowledge(board_id, action_id, body.status, body.detail)
            except KeyError as error:
                raise HTTPException(404, str(error)) from error
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            service.engine.update_action_results(command["incident_id"], {"sound": {
                "status": command["state"], "action_id": command["id"], "output": "station", "detail": command["detail"]}})
            bus.publish("facility.updated", {}, source=service.conn)
            return command

    @routes.get("/api/facility")
    async def state():
        async with service.lock:
            await service.tick()
            return service.engine.snapshot()

    @routes.post("/api/facility/rules/{rule_id}")
    async def save_rule(rule_id: str, body: FacilityRule):
        async with service.lock:
            try:
                if rule_id != body.id:
                    raise ValueError("Rule id differs from route")
                service.validate_rule(body)
                result = service.engine.save_rule(body)
            except KeyError as error:
                raise HTTPException(404, str(error)) from error
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            bus.publish("facility.updated", {}, source=service.conn)
            return result

    @routes.post("/api/facility/readings")
    async def reading(body: FacilityReading):
        async with service.lock:
            try:
                return await service.ingest(body)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error

    @routes.post("/api/facility/incidents/{incident_id}/acknowledge")
    async def acknowledge(incident_id: str):
        async with service.lock:
            try:
                result = service.engine.acknowledge(incident_id)
            except KeyError as error:
                raise HTTPException(404, str(error)) from error
            bus.publish("facility.updated", {}, source=service.conn)
            return result

    @routes.post("/api/telemetry")
    async def telemetry(body: Telemetry):
        metric = metric_for(body.key)
        if body.board_id not in config.BOARDS:
            raise HTTPException(404, "Unknown board")
        if metric is None:
            return {"accepted": False, "metric": None}
        try:
            parsed = FacilityReading.model_validate({"event_id": body.event_id or uuid4().hex,
                "board_id": body.board_id, "metric": metric, "value": body.value,
                "timestamp": body.timestamp or service.engine.snapshot()["evaluated_at"], "simulation": body.simulation})
            async with service.lock:
                result = await service.ingest(parsed)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return {"accepted": True, "metric": metric, **result}

    @routes.get("/api/air-quality")
    async def air_quality():
        async with service.lock:
            snapshot = service.engine.snapshot()
        latest = {}
        for reading in sorted(snapshot["readings"], key=lambda r: r["timestamp"]):
            if reading["board_id"] == "kitchen" and not reading["simulation"]:
                latest[reading["metric"]] = reading
        from datetime import datetime
        now = datetime.fromisoformat(snapshot["evaluated_at"])
        fresh = {key: value for key, value in latest.items() if (now - datetime.fromisoformat(value["timestamp"])).total_seconds() < 30}
        co2 = fresh.get("co2_ppm", {}).get("value")
        level = "unknown" if co2 is None else "poor" if co2 > 1200 else "moderate" if co2 >= 800 else "good"
        return {"level": level, "readings": {key: fresh.get(key, {}).get("value") for key in ("temperature_c", "humidity_pct", "co2_ppm")},
                "updated_at": max((r["timestamp"] for r in fresh.values()), default=None)}

    return routes
