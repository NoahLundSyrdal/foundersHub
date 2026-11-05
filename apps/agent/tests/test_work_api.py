"""Operator API and legacy adapter acceptance paths; no network or real side effects."""
from datetime import UTC, datetime, timedelta
import inspect

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest

from office import agent, config, plugins
from office.api import create_app
from office_procurement.serialization import proposal_fingerprint


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setitem(config.OFFICE, "require_approval", True)
    with TestClient(create_app(":memory:")) as client:
        yield client


def post(api, path, body=None, status=200):
    result = api.post(path, json=body or {})
    assert result.status_code == status, result.text
    return result.json()


def create(api, **overrides):
    return post(api, "/api/work", {"event_key": "work-create", "title": "Meeting commitment", "outcome": "Accepted report delivered", "owner": "operations", "template_key": "meeting_action", **overrides})


def action(api, work, action, **overrides):
    status = overrides.pop("status", 200)
    return post(api, f"/api/work/{work['id']}/actions", {"event_key": f"action-{work['id']}-{work['version']}", "expected_version": work["version"], "action": action, **overrides}, status=status)


def evidence(api, work, step_id, kind, **overrides):
    status = overrides.pop("status", 200)
    return post(api, f"/api/work/{work['id']}/evidence", {"event_key": f"evidence-{work['id']}-{work['version']}", "expected_version": work["version"], "step_id": step_id, "kind": kind, "summary": "Responsible owner confirmed", "source": "Local operator record", "recorded_by": "local-operator", **overrides}, status=status)


def latest(api, work):
    response = api.get(f"/api/work/{work['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def coffee(api):
    task = post(api, "/api/input", {"board_id": "kitchen", "text": "We need coffee"})
    assert task["status"] == "awaiting_approval"
    work = next(w for w in api.get("/api/work").json()["items"] if w["links"].get("task_id") == task["id"])
    return task, work


def approve_coffee(api, task):
    result = post(api, f"/api/requests/{task['id']}/approve", {"proposal_fingerprint": proposal_fingerprint(task["proposal"])})
    assert result["status"] == "ordered"
    return result


def test_http_work_idempotency_versions_and_evidence_closure(api):
    work = create(api)
    assert create(api) == work
    post(api, "/api/work", {"event_key": "work-create", "title": "Meeting commitment", "outcome": "Changed content", "template_key": "meeting_action"}, status=409)
    action(api, work, "close", status=409)
    action(api, work, "complete_step", step_id="deliver", status=409)
    version1 = work
    work = evidence(api, work, "deliver", "accepted_deliverable")
    action(api, version1, "assign", owner="someone-else", status=409)
    work = action(api, work, "complete_step", step_id="deliver")
    assert work["status"] != "completed"
    work = action(api, work, "close")
    assert work["status"] == "completed"
    assert work["next_review_at"] is None


def test_step_dependencies_and_missing_evidence_cannot_be_bypassed(api):
    work = create(api, template_key="onboarding", outcome="New colleague ready")
    work = evidence(api, work, "orientation", "orientation_confirmation")
    action(api, work, "complete_step", step_id="orientation", status=409)
    for step in ("equipment", "access"):
        work = evidence(api, work, step, step + "_confirmation")
        work = action(api, work, "complete_step", step_id=step)
    work = action(api, work, "complete_step", step_id="orientation")
    assert action(api, work, "close")["status"] == "completed"


def test_common_work_survives_api_restart_and_replay(tmp_path):
    path = str(tmp_path / "restart.sqlite")
    with TestClient(create_app(path)) as first:
        work = create(first)
    with TestClient(create_app(path)) as second:
        assert latest(second, work) == work
        assert create(second) == work
        assert len(second.get("/api/work").json()["items"]) == 1


def test_linked_replenishment_receipt_updates_inventory_once_then_requires_close(api):
    task, work = coffee(api)
    assert work["status"] == "awaiting_approval"
    evidence(api, work, "order", "order_confirmation", status=409)
    evidence(api, work, "inventory", "inventory_update", status=409)
    action(api, work, "complete_step", step_id="order", status=409)
    action(api, work, "simulate_step", step_id="order", status=409)
    post(api, f"/api/work/{work['id']}/decisions", {"event_key": "bypass", "expected_version": work["version"], "step_id": "order", "approve": True, "proposal_fingerprint": "anything"}, status=409)
    approve_coffee(api, task)
    work = latest(api, work)
    assert work["steps"][0]["status"] == "completed"
    assert work["status"] == "waiting"
    assert api.get("/api/work/inventory").json()["items"] == []
    action(api, work, "close", status=409)
    work = evidence(api, work, "receipt", "receipt")
    work = action(api, work, "complete_step", step_id="receipt")
    assert all(s["status"] == "completed" for s in work["steps"])
    assert work["status"] != "completed"
    for _ in range(3):
        latest(api, work)
        api.get("/api/work")
    inventory = api.get("/api/work/inventory").json()
    assert inventory["items"][0]["received_quantity"] == task["proposal"]["quantity"]
    assert inventory["includes_consumption"] is False
    work = latest(api, work)
    assert len([e for e in work["evidence"] if e["kind"] == "inventory_update"]) == 1
    assert action(api, work, "close")["status"] == "completed"
    assert len(api.get("/api/state").json()["orders"]) == 1


@pytest.mark.parametrize("legacy_route", ["delivered", "resolve"])
def test_legacy_delivery_backfills_receipt_inventory_exactly_once(api, legacy_route):
    task, work = coffee(api)
    approve_coffee(api, task)
    order = api.get("/api/state").json()["orders"][0]
    if legacy_route == "delivered":
        for _ in range(2):
            post(api, f"/api/orders/{order['id']}/delivered")
    else:
        post(api, f"/api/requests/{task['id']}/resolve")
    work = latest(api, work)
    assert all(s["status"] == "completed" for s in work["steps"])
    assert work["status"] != "completed"
    assert len(work["evidence"]) == 3
    assert api.get("/api/work/inventory").json()["items"][0]["received_quantity"] == order["quantity"]
    assert api.app.state.conn.execute("SELECT COUNT(*) FROM office_inventory_receipts").fetchone()[0] == 1


def test_resolved_maintenance_ticket_requires_independent_verification(api):
    task = post(api, "/api/input", {"board_id": "printer", "text": "Printer is broken"})
    ticket = api.get("/api/state").json()["tickets"][0]
    work = next(w for w in api.get("/api/work").json()["items"] if w["links"].get("task_id") == task["id"])
    post(api, f"/api/tickets/{ticket['id']}/resolve")
    work = latest(api, work)
    assert work["steps"][0]["status"] == "completed"
    assert work["steps"][1]["status"] != "completed"
    action(api, work, "close", status=409)
    work = evidence(api, work, "verify", "verification")
    work = action(api, work, "complete_step", step_id="verify")
    assert action(api, work, "close")["status"] == "completed"


def test_sensor_recovery_does_not_prove_maintenance_performed(api):
    now = datetime.now(UTC)
    rule = next(r for r in api.get("/api/facility").json()["rules"] if r["id"] == "co2-high")
    post(api, "/api/facility/rules/co2-high", {**rule, "min_samples": 1, "min_duration_seconds": 0})
    for offset, value in enumerate((1400, 900)):
        result = post(api, "/api/facility/readings", {"event_id": f"co2-{offset}", "board_id": "kitchen", "metric": "co2_ppm", "value": value, "timestamp": (now + timedelta(seconds=offset)).isoformat(), "simulation": True})
    assert result["incidents"][0]["status"] == "recovered"
    work = api.get("/api/work").json()["items"][0]
    assert work["template_key"] == "maintenance"
    assert work["status"] != "completed"
    assert all(s["status"] != "completed" for s in work["steps"])
    assert not work["evidence"]


def test_static_routes_notifications_and_routine_controls(api):
    work = create(api, next_review_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    for path, key in [("templates", "templates"), ("routines", "routines"), ("inventory", "items")]:
        result = api.get(f"/api/work/{path}")
        assert result.status_code == 200, result.text
        assert key in result.json()
    notifications = api.get("/api/work/notifications").json()["notifications"]
    assert len(notifications) == 1
    assert notifications[0]["delivery"] == "local_only"
    assert notifications[0]["work_id"] == work["id"]
    post(api, f"/api/work/notifications/{notifications[0]['id']}/acknowledge")
    post(api, f"/api/work/notifications/{notifications[0]['id']}/acknowledge")
    assert api.get("/api/work/notifications").json()["notifications"][0]["acknowledged_at"]
    routine = post(api, "/api/work/routines", {"event_key": "routine-1", "name": "Weekly report", "timezone": "Europe/Oslo", "local_time": "09:00", "starts_on": "2026-11-05", "work": {"event_key": "unused-occurrence-key", "title": "Weekly report", "outcome": "Report accepted", "template_key": "meeting_action"}, "enabled": False})
    assert routine["next_run_at"] is None
    enabled = post(api, f"/api/work/routines/{routine['id']}/enabled", {"enabled": True})
    assert enabled["next_run_at"]


def test_shared_connection_http_routes_and_registered_tools_are_async(api):
    assert inspect.iscoroutinefunction(agent.context)
    for route in api.app.routes:
        if isinstance(route, APIRoute):
            assert inspect.iscoroutinefunction(route.endpoint), route.path
    for plugin in plugins.PLUGINS:
        for name, tool in plugin.toolset.tools.items():
            assert inspect.iscoroutinefunction(tool.function), name


def test_confirmed_service_booking_is_not_stock_or_verified_repair(api):
    task = post(api, "/api/input", {"board_id": "bathroom", "text": "Book a plumber"})
    assert task["proposal"]["fulfillment_kind"] == "service"
    post(api, f"/api/requests/{task['id']}/approve", {"proposal_fingerprint": proposal_fingerprint(task["proposal"])})
    work = next(w for w in api.get("/api/work").json()["items"] if w["links"].get("task_id") == task["id"])
    assert work["template_key"] == "maintenance"
    assert "inventory" not in {step["id"] for step in work["steps"]}
    assert next(step for step in work["steps"] if step["id"] == "verify")["status"] != "completed"
    assert api.get("/api/work/inventory").json()["items"] == []
    action(api, work, "close", status=409)


def test_invalid_routine_template_does_not_poison_future_ticks(api):
    post(api, "/api/work/routines", {"event_key": "bad-template", "name": "Invalid routine", "timezone": "Europe/Oslo",
        "local_time": "09:00", "starts_on": "2026-11-05", "work": {"event_key": "unused", "title": "Bad", "outcome": "Bad", "template_key": "missing"}}, status=409)
    assert api.get("/api/work").status_code == 200
    assert api.get("/api/work/routines").json()["routines"] == []


def test_uncertain_linked_purchase_cannot_be_hidden_by_cancellation(api):
    task, work = coffee(api)
    api.app.state.conn.execute("INSERT INTO procurement_attempts VALUES(?,?,?,'uncertain',NULL)", (task["id"], "uncertain-attempt", "{}"))
    for operation in ("cancel", "fail"):
        action(api, work, operation, reason="Hide uncertain purchase", status=409)
    assert latest(api, work)["next_review_at"] is not None


def test_manual_repair_confirmation_updates_linked_ticket_without_verifying_it(api):
    now = datetime.now(UTC)
    rule = next(r for r in api.get("/api/facility").json()["rules"] if r["id"] == "co2-high")
    post(api, "/api/facility/rules/co2-high", {**rule, "min_samples": 1, "min_duration_seconds": 0})
    post(api, "/api/facility/readings", {"event_id": "needs-inspection", "board_id": "kitchen", "metric": "co2_ppm", "value": 1400, "timestamp": now.isoformat(), "simulation": True})
    work = api.get("/api/work").json()["items"][0]
    work = evidence(api, work, "repair", "repair")
    work = action(api, work, "complete_step", step_id="repair")
    assert api.get("/api/state").json()["tickets"][0]["status"] == "resolved"
    assert next(s for s in work["steps"] if s["id"] == "verify")["status"] != "completed"
    work = action(api, work, "wait", reason="Waiting for owner verification", next_review_at=(now + timedelta(days=1)).isoformat())
    assert latest(api, work)["status"] == "waiting"
    assert latest(api, work)["version"] == work["version"]
