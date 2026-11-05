"""Sourcing, severity and durable exact-quote approval for the existing office agent."""
import json
import time

from office import brain, bus, config, db
from office_procurement.config import configuration
from office_procurement.planning import plan_purchase, needs_approval, fixture_offers, offer_total
from office_procurement.serialization import proposal_fingerprint, epoch
from office_procurement.models import WorkerInput, WorkerOutput, Interpretation
from office_procurement.worker import run_worker, validate_decision, Context, search_item


class ProcurementConflict(Exception):
    pass


def ensure_schema(conn):
    schema = """
      CREATE TABLE IF NOT EXISTS procurement_context(task_id TEXT PRIMARY KEY,body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS procurement_attempts(task_id TEXT PRIMARY KEY,tool_call_id TEXT NOT NULL UNIQUE,proposal TEXT NOT NULL,state TEXT NOT NULL,reference TEXT);
      CREATE TABLE IF NOT EXISTS procurement_alerts(id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,severity TEXT NOT NULL,title TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL,acknowledged_at TEXT);
    """
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)


def get_context(conn, task_id):
    ensure_schema(conn)
    row = db.row(conn, "SELECT body FROM procurement_context WHERE task_id=?", task_id)
    return json.loads(row["body"]) if row else None


def oda_attempt(conn, task_id):
    """Inspect persisted cart work without initializing Oda or touching its account."""
    if not db.row(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oda_cart_attempts'"):
        return None
    return db.row(conn, "SELECT * FROM oda_cart_attempts WHERE task_id=?", task_id)


def execution_attempted(conn, task_id):
    ensure_schema(conn)
    return bool(db.row(conn, "SELECT 1 FROM procurement_attempts WHERE task_id=?", task_id) or oda_attempt(conn, task_id))


def store_context(conn, task_id, value):
    conn.execute("INSERT INTO procurement_context VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET body=excluded.body",
                 (task_id, proposal_fingerprint(value)))


def enrich_task(conn, task):
    value = get_context(conn, task["id"])
    return task | {key: value.get(key) for key in ("assessment", "proposal", "research", "approval_required", "approval", "grocery")} if value else task


def approval_route(proposal, rules) -> dict:
    """Who must approve, from the company brain's routing table (office/brain.py). Application code decides the
    type; the model never picks the approver. A repair/visit is `contractor`, anything above the cap is
    `purchase_over_cap`, everything else `purchase`."""
    cap = rules["max_order_nok"]
    if proposal.get("fulfillment_kind") == "service":
        kind, reason = "contractor", "Booking a paid service visit"
    elif proposal["total_nok"] > cap:
        kind, reason = "purchase_over_cap", f"Above the office cap of {cap:g} NOK per order"
    else:
        kind, reason = "purchase", "Office policy requires a human to approve this order"
    return {"type": kind, "approver": brain.approver(kind), "reason": reason}


def current_configuration():
    return configuration(config.OFFICE)


def open_requests(conn, exclude_id):
    requests = []
    for row in db.rows(conn, "SELECT p.task_id,p.body,t.board_id,t.status FROM procurement_context p JOIN tasks t ON t.id=p.task_id WHERE p.task_id<>?", exclude_id):
        value = json.loads(row["body"])
        active = db.row(conn, "SELECT 1 FROM orders WHERE task_id=? AND status='simulated'", row["task_id"])
        attempted = db.row(conn, "SELECT state FROM procurement_attempts WHERE task_id=?", row["task_id"])
        status = "ordered" if active else "failed" if attempted and attempted["state"] != "confirmed" else {
            "running": "processing", "awaiting_approval": "awaiting_approval", "failed": "failed"}.get(row["status"], "resolved")
        requests.append({"id": row["task_id"], "station_id": row["board_id"], "item_id": value["interpretation"]["item_id"],
                         "status": status, "proposal": value.get("proposal")})
    return requests


async def prepare_need(conn, task_id: str, text: str | None, *, decision: Interpretation | None = None):
    ensure_schema(conn)
    old = get_context(conn, task_id)
    if text is None:
        if not old:
            raise ProcurementConflict("Stored assessment is missing; create a new request.")
        return old
    if execution_attempted(conn, task_id):
        raise ProcurementConflict("Execution was already attempted; reconcile its outcome instead of reinterpreting this task.")
    if old and (old.get("proposal") or (old.get("grocery") or {}).get("proposal")):
        raise ProcurementConflict("This task already has a quote. Create a new request instead of changing an approval.")
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    stations, products, templates, rules = current_configuration()
    station = next((s for s in stations if s["id"] == task["board_id"]), None)
    if not station:
        raise ProcurementConflict("Board has no configured procurement station.")
    requests = open_requests(conn, task_id)
    provider = "mock" if config.MODEL == "mock" else "anthropic"
    from office import oda_food
    if provider == "anthropic" and not config.MODEL.startswith("anthropic:"):
        raise ProcurementConflict("Procurement supports mock or the configured Anthropic provider.")
    request = WorkerInput.model_validate({
        "text": text, "station": station,
        "catalogue": [p for p in products if p["station_id"] == station["id"] and p["id"] in station["item_ids"]],
        "policy": {k: rules[k] for k in ("max_order_nok", "require_approval", "preferences")},
        "open_requests": [{k: r[k] for k in ("id", "station_id", "item_id", "status")} for r in requests[:100]],
        "provider": provider, "model": config.MODEL.split(":", 1)[1] if provider == "anthropic" else None,
        "grocery_enabled": oda_food.enabled(),
    })
    direct = decision is not None
    if decision is None:
        result = await run_worker(request)
    else:
        decision = validate_decision(request, decision)
        context = Context(request)
        if decision.item_id:
            await search_item(context, decision.item_id)
        result = WorkerOutput(interpretation=decision, research=context.research, progress=context.progress)
    decision = result.interpretation.model_dump()
    value = {"interpretation": decision, "assessment": decision["assessment"], "research": [r.model_dump(mode="json") for r in result.research],
             "proposal": None, "approval_required": True, "prepared_at": db.now()}
    if direct:
        value["source"] = "validated-need"
    if decision["intent"] == "grocery":
        value = await oda_food.service(conn).prepare(task_id, value)
    elif decision["intent"] == "purchase" or (decision["intent"] == "maintenance" and decision["item_id"]):
        plan = plan_purchase(station, decision["item_id"], decision["quantity"], requests, products, templates, rules, time.time())
        if plan["kind"] == "proposal":
            value["proposal"] = plan["proposal"]
            value["approval_required"] = needs_approval({"assessment": value["assessment"], "proposal": value["proposal"]}, rules)
            value["approval"] = approval_route(value["proposal"], rules)
        elif plan["kind"] == "duplicate":
            value["duplicate_task_id"] = plan["request"]["id"]
        else:
            value["question"] = plan["question"]
    store_context(conn, task_id, value)
    assessment = value["assessment"]
    if assessment["severity"] in {"urgent", "critical"}:
        conn.execute("""INSERT INTO procurement_alerts VALUES(?,?,?,?,?,?,NULL)
          ON CONFLICT(request_id) DO UPDATE SET severity=excluded.severity,title=excluded.title,detail=excluded.detail,
          acknowledged_at=CASE WHEN procurement_alerts.severity<>excluded.severity THEN NULL ELSE procurement_alerts.acknowledged_at END""",
                     (task_id, task_id, assessment["severity"], decision["summary"], assessment["reason"], db.now()))
    bus.publish("requests.updated", {"request_id": task_id})
    for step in result.progress:
        bus.publish("procurement.progress", {"task_id": task_id, **step.model_dump()})
    return value


def validate_quote(conn, task_id):
    value = get_context(conn, task_id)
    p = value and value.get("proposal")
    if not p:
        raise ProcurementConflict("No executable quote was prepared for this task.")
    try:
        if epoch(p["expires_at"]) <= time.time():
            raise ProcurementConflict("Quote expired; create a new request and review a fresh quote.")
        stations, products, templates, rules = current_configuration()
        task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
        station = next(s for s in stations if s["id"] == task["board_id"])
        q = next(o for o in fixture_offers(templates, rules, time.time()) if o["id"] == p["offer_id"])
        reviewed = next(o["offer"] for o in p["sourcing"]["alternatives"] if o["offer"]["id"] == p["offer_id"])
        unchanged = {k: v for k, v in q.items() if k != "expires_at"} == {k: v for k, v in reviewed.items() if k != "expires_at"}
        product, quantity = p["product"], p["quantity"]
        if not (unchanged and reviewed["expires_at"] == p["expires_at"] and q["available"] and
                q["item_id"] == value["interpretation"]["item_id"] == product["id"] and
                q["station_id"] == station["id"] == product["station_id"] and q["item_id"] in station["item_ids"] and
                any(x["id"] == product["id"] and x["station_id"] == station["id"] for x in products) and
                type(quantity) is int and 1 <= quantity <= 20 and p["sourcing"]["selected_offer_id"] == q["id"] and
                product["price_nok"] == q["unit_price_nok"] and product["name"] == q["name"] and product["supplier"] == q["supplier"] and
                p["fulfillment_kind"] == q["fulfillment_kind"] and p["currency"] == "NOK" and p["simulation"] is True and
                product["delivery_estimate"] == f"Simulation: {q['delivery_hours']} hours (not verified)" and
                p["total_nok"] == offer_total(q, quantity) and
                q["quality_score"] >= rules["minimum_quality"] and q["delivery_hours"] <= rules["max_delivery_hours"]):
            raise ProcurementConflict("Quote or policy changed; create a new request and review it again.")
        return value, rules
    except (KeyError, StopIteration, TypeError, ValueError) as error:
        raise ProcurementConflict("Quote is invalid or changed; create a new request.") from error


def approval_metadata(value, person_name):
    p = value["proposal"]
    route = value.get("approval") or approval_route(p, current_configuration()[3])
    return {"summary": f"{p['quantity']} × {p['product']['name']} = {p['total_nok']:g} NOK (simulated)",
            "total_nok": p["total_nok"], "requested_by": person_name, "proposal": p,
            "proposal_fingerprint": proposal_fingerprint(p), "assessment": value["assessment"],
            "approval_type": route["type"], "approver": route["approver"], "approval_reason": route["reason"]}


def release_quote(conn, task_id):
    """Nothing was attempted, so drop the quote: it must not block the item or look like an order."""
    if execution_attempted(conn, task_id):
        return
    value = get_context(conn, task_id)
    if value:
        value["proposal"] = None
        value.pop("approved_fingerprint", None)
        grocery = value.get("grocery")
        if isinstance(grocery, dict) and grocery.get("proposal"):
            grocery.update(proposal=None, state="failed", cart_url=None,
                           question="Preparation stopped before any cart update. Submit the request again for a fresh basket.")
        value["approval_required"] = True
        store_context(conn, task_id, value)


def requote_task(conn, task_id, fingerprint):
    task = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    value = get_context(conn, task_id)
    if not task or task["status"] != "awaiting_approval" or not value or not value.get("proposal"):
        raise ProcurementConflict("Only an unexecuted proposal awaiting approval can be refreshed.")
    if fingerprint != proposal_fingerprint(value["proposal"]):
        raise ProcurementConflict("Proposal changed; review it again.")
    if db.row(conn, "SELECT 1 FROM procurement_attempts WHERE task_id=?", task_id):
        raise ProcurementConflict("Execution was attempted; reconcile it before refreshing.")
    stations, products, templates, rules = current_configuration()
    station = next(s for s in stations if s["id"] == task["board_id"])
    decision = value["interpretation"]
    plan = plan_purchase(station, decision["item_id"], decision["quantity"], open_requests(conn, task_id),
                         products, templates, rules, time.time())
    if plan["kind"] != "proposal":
        raise ProcurementConflict("No replacement quote is eligible; administrator review is needed.")
    revision = value["proposal"].get("quote_revision", 0) + 1
    value["proposal"] = plan["proposal"] | {"quote_revision": revision}
    value["approval"] = approval_route(value["proposal"], rules)
    value.pop("approved_fingerprint", None)
    value["approval_required"] = True
    pending = json.loads(task["pending"])
    for call in pending:
        if call["tool"] == "place_order":
            call["details"] = approval_metadata(value, task["person"])
    store_context(conn, task_id, value)
    conn.execute("UPDATE tasks SET pending=?,reply=?,updated_at=? WHERE id=?",
                 (json.dumps(pending), "Fresh quote ready for review (simulated).", db.now(), task_id))
    bus.publish("requests.updated", {"request_id": task_id})
    return enrich_task(conn, db.task_view(db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)))


def authorize_pending(conn, task_id, pending, fingerprint=None):
    calls = [p for p in pending if p["tool"] == "place_order"]
    if not calls:
        return
    value, _ = validate_quote(conn, task_id)
    current = proposal_fingerprint(value["proposal"])
    if fingerprint is not None and fingerprint != current:
        raise ProcurementConflict("Proposal changed; review it again.")
    for p in calls:
        if p["details"].get("proposal_fingerprint") != current or proposal_fingerprint(p["details"].get("proposal")) != current:
            raise ProcurementConflict("Pending approval does not match the stored quote.")
        if p["args"].get("product_id") != value["proposal"]["product"]["id"] or p["args"].get("quantity", 1) != value["proposal"]["quantity"]:
            raise ProcurementConflict("Pending tool arguments do not match the reviewed quote.")
    value["approved_fingerprint"] = current
    store_context(conn, task_id, value)


def list_alerts(conn):
    ensure_schema(conn)
    return db.rows(conn, "SELECT * FROM procurement_alerts ORDER BY created_at DESC")


def acknowledge_alert(conn, alert_id):
    ensure_schema(conn)
    alert = db.row(conn, "SELECT * FROM procurement_alerts WHERE id=?", alert_id)
    if not alert:
        raise ProcurementConflict("Alert not found.")
    conn.execute("UPDATE procurement_alerts SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=?", (db.now(), alert_id))
    bus.publish("requests.updated", {"request_id": alert["request_id"]})
    return db.row(conn, "SELECT * FROM procurement_alerts WHERE id=?", alert_id)
