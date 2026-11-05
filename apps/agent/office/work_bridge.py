"""Map existing domain records to outcome tracking without replacing their state.

The purchase step can only be satisfied by the existing order boundary. Receipt
confirmation records a simulated inventory movement once per order. A recovered
sensor or resolved ticket never establishes that a repair was verified.
"""
import hashlib
from datetime import timedelta

from office import config, db, procurement_workflow
from office.work_models import StepCreate, WorkCreate
from office.work_service import TERMINAL, WorkConflict, canonical, stamp


class WorkBridge:
    def __init__(self, conn, service):
        self.conn, self.service = conn, service
        conn.execute("CREATE TABLE IF NOT EXISTS office_work_links(source_id TEXT PRIMARY KEY,work_id TEXT NOT NULL,signature TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS office_inventory_receipts(
            order_id TEXT PRIMARY KEY,work_id TEXT NOT NULL,item_id TEXT NOT NULL,location_id TEXT NOT NULL,
            quantity INTEGER NOT NULL,simulation INTEGER NOT NULL,recorded_at TEXT NOT NULL)""")

    def inventory(self):
        return [dict(r) for r in self.conn.execute("""SELECT item_id,location_id,simulation,
            SUM(quantity) AS received_quantity FROM office_inventory_receipts
            GROUP BY item_id,location_id,simulation ORDER BY location_id,item_id""")]

    def _evidence(self, work, step_id, kind, summary, source):
        if not any(e["step_id"] == step_id and e["kind"] == kind for e in work["evidence"]):
            work["evidence"].append({"id": hashlib.sha256(f"{work['id']}:{step_id}:{kind}".encode()).hexdigest()[:24],
                "step_id": step_id, "kind": kind, "summary": summary, "source": source,
                "recorded_by": "domain-adapter", "created_at": stamp(self.service._now()), "simulation": True})

    def _step(self, work, id):
        return next(s for s in work["steps"] if s["id"] == id)

    def _save(self, work, detail):
        work["version"] += 1
        work["updated_at"] = stamp(self.service._now())
        work["history"].append({"at": work["updated_at"], "action": "domain_sync", "detail": detail})
        self.service._save(work)

    def sync(self):
        changed = False
        # Ignore conversational/input-only tasks until a concrete domain outcome exists.
        for task in db.rows(self.conn, "SELECT * FROM tasks ORDER BY rowid"):
            value = procurement_workflow.get_context(self.conn, task["id"]) or {}
            proposal = value.get("proposal")
            order = db.row(self.conn, "SELECT * FROM orders WHERE task_id=? ORDER BY rowid DESC LIMIT 1", task["id"])
            ticket = db.row(self.conn, "SELECT * FROM tickets WHERE task_id=? ORDER BY rowid DESC LIMIT 1", task["id"])
            is_service = bool(ticket or (proposal or {}).get("fulfillment_kind") == "service" or
                (order and config.PRODUCTS.get(order["product_id"], {}).get("fulfillment_kind") == "service"))
            source_id = "task:" + task["id"]
            link = db.row(self.conn, "SELECT * FROM office_work_links WHERE source_id=?", source_id)
            attempt = db.row(self.conn, "SELECT state FROM procurement_attempts WHERE task_id=?", task["id"])
            if link and not order and not ticket and not attempt and task["status"] in {"done", "failed"}:
                # A declined quote or failure before execution must not leave an
                # old approval waiting forever. This never establishes delivery.
                work = self.service.get(link["work_id"])
                if work["status"] not in TERMINAL:
                    work["status"] = "failed" if task["status"] == "failed" else "cancelled"
                    work["waiting_reason"] = None
                    work["next_review_at"] = None
                    self._save(work, "Purchasing ended before execution; no order was placed")
                    changed = True
                continue
            if not proposal and not order and not ticket:
                continue
            signature = canonical({"status": task["status"], "proposal": proposal, "order": order, "ticket": ticket})
            with self.service._atomic():
                if link:
                    work = self.service.get(link["work_id"])
                else:
                    owner = ticket["team"] if ticket else config.BOARDS[task["board_id"]].responsible_team
                    if not is_service and (proposal or order):
                        steps = [StepCreate(id="order", title="Confirm order through the purchasing workflow", owner=owner,
                            required_evidence=["order_confirmation"], action_snapshot=proposal or {}),
                            StepCreate(id="receipt", title="Confirm full receipt at the location", owner=owner,
                                depends_on=["order"], required_evidence=["receipt"]),
                            StepCreate(id="inventory", title="Record received stock", owner=owner,
                                depends_on=["receipt"], required_evidence=["inventory_update"])]
                        item_name = proposal["product"]["name"] if proposal else order["product_id"]
                        title, outcome, template = f"Replenish {item_name}", f"{item_name} received at {task['board_id']} and received inventory updated", "replenishment"
                    else:
                        steps = [StepCreate(id="repair", title="Confirm repair or maintenance performed", owner=owner, required_evidence=["repair"]),
                                 StepCreate(id="verify", title="Verify the result", owner=owner, depends_on=["repair"], required_evidence=["verification"])]
                        title = ticket["summary"] if ticket else (proposal or {}).get("product", {}).get("name", "Service follow-through")
                        outcome, template = "Maintenance result checked and accepted by its accountable owner", "maintenance"
                    assessment = value.get("assessment") or {}
                    work = self.service.create(WorkCreate(event_key="domain-" + task["id"], title=title[:2000], outcome=outcome,
                        owner=owner, template_key=template, context={"location_id": task["board_id"]},
                        priority=assessment.get("severity", "routine"), steps=steps, simulation=True))
                    work["links"] = {"task_id": task["id"]}
                    self.conn.execute("INSERT INTO office_work_links VALUES(?,?,?)", (source_id, work["id"], ""))
                    changed = True
                if work["status"] in TERMINAL:
                    continue
                previous = canonical(work)
                if order and work["template_key"] == "maintenance":
                    work["links"]["order_id"] = order["id"]
                    work["context"]["simulated_service_reference"] = order["ref"]
                    if link is None or link["signature"] != signature:
                        work["status"] = "waiting"
                        work["waiting_reason"] = "Service arrangement confirmed in simulation; work and verification still required"
                elif order:
                    work["links"]["order_id"] = order["id"]
                    self._evidence(work, "order", "order_confirmation", f"Simulated order confirmed: {order['ref']}", "orders:" + order["id"])
                    self._step(work, "order")["status"] = "completed"
                    if order["status"] == "delivered" or self._step(work, "receipt")["status"] == "completed":
                        self._evidence(work, "receipt", "receipt", "Full simulated delivery confirmed by local operator", "orders:" + order["id"])
                        self._step(work, "receipt")["status"] = "completed"
                        self.conn.execute("INSERT OR IGNORE INTO office_inventory_receipts VALUES(?,?,?,?,?,1,?)",
                            (order["id"], work["id"], order["product_id"], task["board_id"], order["quantity"], stamp(self.service._now())))
                        self.conn.execute("UPDATE orders SET status='delivered' WHERE id=?", (order["id"],))
                        self._evidence(work, "inventory", "inventory_update", f"Recorded {order['quantity']} received units of {order['product_id']} (simulated)", "inventory:" + order["id"])
                        self._step(work, "inventory")["status"] = "completed"
                    if link is None or link["signature"] != signature:
                        work["status"] = "active" if self._step(work, "receipt")["status"] == "completed" else "waiting"
                        work["waiting_reason"] = None if work["status"] == "active" else "Waiting for receipt confirmation"
                elif proposal and (link is None or link["signature"] != signature):
                    work["status"] = "awaiting_approval" if task["status"] == "awaiting_approval" else "blocked"
                    work["waiting_reason"] = "Review exact proposal in purchasing workflow" if work["status"] == "awaiting_approval" else "Review purchasing outcome before continuing"
                if ticket and any(s["id"] == "repair" for s in work["steps"]):
                    work["links"]["ticket_id"] = ticket["id"]
                    was_repaired = self._step(work, "repair")["status"] == "completed"
                    if self._step(work, "repair")["status"] == "completed":
                        self.conn.execute("UPDATE tickets SET status='resolved' WHERE id=?", (ticket["id"],))
                    if ticket["status"] == "resolved":
                        self._evidence(work, "repair", "repair", "Local maintenance ticket marked resolved; independent verification still required", "tickets:" + ticket["id"])
                        self._step(work, "repair")["status"] = "completed"
                        if not was_repaired:
                            work["status"], work["waiting_reason"] = "active", None
                if canonical(work) != previous:
                    work["next_review_at"] = work["next_review_at"] or stamp(self.service._now() + timedelta(days=1))
                    self._save(work, "Updated from existing purchasing or maintenance records")
                    changed = True
                self.conn.execute("UPDATE office_work_links SET signature=? WHERE source_id=?", (signature, source_id))
        return changed

    def guard(self, work_id, operation, body):
        work = self.service.get(work_id)
        if not work["links"].get("task_id"):
            return
        attempt = db.row(self.conn, "SELECT state FROM procurement_attempts WHERE task_id=?", work["links"]["task_id"])
        if operation == "action" and body.action in {"cancel", "fail"} and attempt and attempt["state"] in {"started", "uncertain"}:
            raise WorkConflict("Reconcile the existing purchasing execution before ending coordination")
        if work["template_key"] == "maintenance" and operation == "action" and body.action in {"start_step", "complete_step", "close"}:
            task_id = work["links"]["task_id"]
            proposal = (procurement_workflow.get_context(self.conn, task_id) or {}).get("proposal")
            if proposal and proposal.get("fulfillment_kind") == "service":
                order = db.row(self.conn, "SELECT id FROM orders WHERE task_id=?", task_id)
                task = db.row(self.conn, "SELECT status FROM tasks WHERE id=?", task_id)
                ticket = db.row(self.conn, "SELECT id FROM tickets WHERE task_id=?", task_id)
                # A suggested quote alone is not a booking: local ticket-only
                # tasks can finish without purchasing a contractor service.
                if not order and (not ticket or task["status"] != "done"):
                    raise WorkConflict("Confirm the service booking in the linked request before completing repair work")
        step_id = getattr(body, "step_id", None)
        if operation == "decision" or (operation == "action" and body.action in {"simulate_step", "reconcile_step", "renew_approval"}):
            raise WorkConflict("Use the linked domain workflow for approval and execution; record actual repair evidence separately")
        if work["template_key"] == "replenishment":
            if step_id in {"order", "inventory"} and operation in {"evidence", "action"}:
                raise WorkConflict("This step is managed by the purchasing and inventory adapters")
