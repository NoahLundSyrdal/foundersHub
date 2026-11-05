"""Notifications are durable, informational, and never replay uncertain sends."""
import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from office import bus, config, db, procurement_workflow
from office.api import create_app
from office.feedback import FeedbackService, WhatsApp, request_event
from office_procurement.serialization import proposal_fingerprint


def settings(**changes):
    return WhatsApp(**({"enabled": True, "account_sid": "AC" + "a" * 32, "token": "test-secret",
                        "sender": "whatsapp:+4712345678", "recipient": "whatsapp:+4787654321",
                        "dashboard_url": "https://office.example"} | changes))


@pytest.fixture
def store():
    conn = db.connect(":memory:")
    procurement_workflow.ensure_schema(conn)
    incidents = []
    facilities = SimpleNamespace(engine=SimpleNamespace(snapshot=lambda: {"incidents": incidents}))
    yield conn, facilities, incidents
    conn.close()


def task(conn, task_id="req-1", status="awaiting_input"):
    conn.execute("INSERT INTO tasks(id,board_id,person,prompt,status,reply,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                 (task_id, "kitchen", "private identity", "private transcript", status, "private reply", db.now(), db.now()))


def approval(conn, task_id="req-1", total=252.02, revision=1):
    proposal = {"kind": "purchase", "quantity": 2, "total_nok": total, "simulation": True,
                "product": {"id": "coffee", "name": "Coffee", "price_nok": 126.01}, "quote_revision": revision}
    procurement_workflow.store_context(conn, task_id, {"proposal": proposal, "assessment": {"severity": "urgent"}})
    conn.execute("UPDATE tasks SET status='awaiting_approval' WHERE id=?", (task_id,))


def service(store, configured=None, transport=None):
    conn, facilities, _ = store
    return FeedbackService(conn, facilities, asyncio.Lock(), configured or settings(), transport)


def test_current_main_grocery_feedback_preserves_real_cart_semantics_and_exact_basket_identity():
    request = {"id": "grocery", "station_id": "kitchen", "status": "awaiting_approval",
               "updated_at": db.now(), "simulation": False, "proposal": None,
               "grocery": {"proposal": {"lines": [{"id": "coffee", "quantity": 1}], "subtotal_nok": 100}}}
    first = request_event(request)
    assert first["simulation"] is False
    assert "does not place or pay an order" in first["detail"]
    assert "simulated" not in first["detail"].lower()
    changed = request | {"grocery": {"proposal": {"lines": [{"id": "coffee", "quantity": 2}], "subtotal_nok": 200}}}
    assert request_event(changed)["id"] != first["id"]


def test_existing_history_is_visible_but_never_sent_and_new_quote_is_exact(store):
    conn, _, _ = store
    task(conn)
    feed = service(store)
    assert feed.snapshot()["events"][0]["whatsapp"]["status"] == "skipped"
    approval(conn)
    feed.sync()
    events = feed.snapshot()["events"]
    event = next(e for e in events if e["kind"] == "approval")
    assert event["whatsapp"]["status"] == "pending"
    assert "2 × Coffee · 252.02 NOK total" in event["detail"]
    assert "Simulated" in event["detail"]
    assert "private" not in json.dumps(events)
    feed.sync()
    assert len(feed.snapshot()["events"]) == 2
    approval(conn, total=300.15, revision=2)
    feed.sync()
    assert len(feed.snapshot()["events"]) == 3
    restarted = service(store)
    assert len(restarted.snapshot()["events"]) == 3
    assert not any(e["whatsapp"]["status"] == "pending" for e in restarted.snapshot()["events"])


def test_provider_acceptance_is_sent_once_and_does_not_place_order(store):
    conn, _, _ = store
    captured = []
    def accept(request):
        captured.append(request)
        return httpx.Response(201, json={"sid": "SM" + "1" * 32, "status": "queued"})
    feed = service(store, transport=httpx.MockTransport(accept))
    task(conn)
    approval(conn)
    feed.sync()
    asyncio.run(feed.deliver_pending())
    asyncio.run(feed.deliver_pending())
    assert len(captured) == 1
    form = parse_qs(captured[0].content.decode())
    assert form["To"] == [settings().recipient]
    assert "https://office.example/?request=req-1" in form["Body"][0]
    assert "252.02 NOK" in form["Body"][0]
    event = feed.snapshot()["events"][0]
    assert event["whatsapp"]["status"] == "sent"
    assert "not confirmed" in event["whatsapp"]["detail"]
    assert db.rows(conn, "SELECT * FROM orders") == []
    assert db.row(conn, "SELECT status FROM tasks WHERE id='req-1'")["status"] == "awaiting_approval"
    restarted = service(store, transport=httpx.MockTransport(accept))
    asyncio.run(restarted.deliver_pending())
    assert len(captured) == 1


def test_pending_approval_for_already_ordered_request_is_skipped(store):
    conn, _, _ = store
    captured = []
    def accept(request):
        captured.append(request)
        return httpx.Response(201, json={"sid": "SM" + "1" * 32})
    feed = service(store, transport=httpx.MockTransport(accept))
    task(conn)
    approval(conn)
    feed.sync()
    conn.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)",
                 ("order-1", "req-1", "call-1", "coffee", 2, 252.02, "simulated", "SIM-TEST", db.now()))
    conn.execute("UPDATE tasks SET status='done' WHERE id='req-1'")
    asyncio.run(feed.deliver_pending())
    assert captured == []
    event = feed.snapshot()["events"][0]
    assert event["kind"] == "approval"
    assert event["whatsapp"]["status"] == "skipped"
    assert "stale or superseded" in event["whatsapp"]["detail"]
    assert db.row(conn, "SELECT COUNT(*) AS count FROM orders")["count"] == 1


def test_superseded_quote_is_skipped_and_only_current_exact_quote_is_sent(store):
    conn, _, _ = store
    captured = []
    def accept(request):
        captured.append(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM" + "1" * 32})
    feed = service(store, transport=httpx.MockTransport(accept))
    task(conn)
    approval(conn)
    feed.sync()
    old_id = feed.snapshot()["events"][0]["id"]
    approval(conn, total=300.15, revision=2)
    feed.sync()
    asyncio.run(feed.deliver_pending())
    assert len(captured) == 1
    assert "300.15 NOK" in captured[0]["Body"][0]
    assert "252.02 NOK" not in captured[0]["Body"][0]
    events = feed.snapshot()["events"]
    old = next(event for event in events if event["id"] == old_id)
    assert old["whatsapp"]["status"] == "skipped"
    assert "exact proposal changed" in old["whatsapp"]["detail"]
    assert next(event for event in events if event["id"] != old_id)["whatsapp"]["status"] == "sent"
    asyncio.run(feed.deliver_pending())
    assert len(captured) == 1


@pytest.mark.parametrize("failure,expected", [("network", "uncertain"), (400, "failed"), (503, "uncertain"), ("invalid", "uncertain")])
def test_failed_or_uncertain_delivery_never_retries(store, failure, expected):
    conn, _, _ = store
    calls = []
    def fail(request):
        calls.append(request)
        if failure == "network":
            raise httpx.ReadTimeout("contains private provider data", request=request)
        if failure == "invalid":
            return httpx.Response(201, json=[])
        return httpx.Response(failure, json={"message": "contains private provider data"})
    transport = httpx.MockTransport(fail)
    feed = service(store, transport=transport)
    task(conn)
    approval(conn)
    feed.sync()
    asyncio.run(feed.deliver_pending())
    restarted = service(store, transport=transport)
    asyncio.run(restarted.deliver_pending())
    assert len(calls) == 1
    assert feed.snapshot()["events"][0]["whatsapp"]["status"] == expected
    assert "private provider data" not in json.dumps(feed.snapshot())


def test_disabled_events_stay_disabled_after_configuration(store):
    conn, _, _ = store
    feed = service(store, configured=settings(enabled=False))
    task(conn)
    approval(conn)
    feed.sync()
    assert feed.snapshot()["events"][0]["whatsapp"]["status"] == "disabled"
    enabled = service(store)
    assert enabled.snapshot()["events"][0]["whatsapp"]["status"] == "disabled"


def test_interrupted_send_is_persisted_before_network_and_never_retried(store):
    conn, _, _ = store
    calls = []
    def interrupt(request):
        calls.append(request)
        assert db.row(conn, "SELECT whatsapp_status FROM feedback_events")["whatsapp_status"] == "uncertain"
        raise asyncio.CancelledError()
    feed = service(store, transport=httpx.MockTransport(interrupt))
    task(conn)
    approval(conn)
    feed.sync()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(feed.deliver_pending())
    restarted = service(store, transport=httpx.MockTransport(interrupt))
    asyncio.run(restarted.deliver_pending())
    assert len(calls) == 1
    assert restarted.snapshot()["events"][0]["whatsapp"]["status"] == "uncertain"


def test_facility_alert_recovery_and_deduplication(store):
    _, _, incidents = store
    feed = service(store)
    incidents.append({"id": "sensor-incident", "status": "active", "board_id": "kitchen", "title": "CO₂ high",
                      "opened_at": db.now(), "recovered_at": None, "severity": "urgent", "simulation": False,
                      "action_results": {"ticket": {"task_id": "inspection-task"}}})
    feed.sync()
    feed.sync()
    assert len(feed.snapshot()["events"]) == 1
    event = feed.snapshot()["events"][0]
    assert event["kind"] == "alert" and event["simulation"] is False
    assert event["request_id"] == "inspection-task"
    incidents[0] |= {"status": "recovered", "recovered_at": db.now()}
    feed.sync()
    assert {e["kind"] for e in feed.snapshot()["events"]} == {"alert", "recovered"}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:password@example.com", "https://example.com/?token=secret", "http://example.com", "http://[bad"])
def test_invalid_dashboard_urls_do_not_enable_transport(url):
    assert settings(dashboard_url=url).configured is False


def test_deep_link_encodes_identifier_and_template_keeps_approval_in_dashboard(store):
    configured = settings(content_sid="HX" + "b" * 32)
    assert configured.link("a&approve=true") == "https://office.example/?request=a%26approve%3Dtrue"
    conn, _, _ = store
    captured = []
    def accept(request):
        captured.append(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM" + "1" * 32})
    feed = service(store, configured, httpx.MockTransport(accept))
    task(conn)
    approval(conn)
    feed.sync()
    asyncio.run(feed.deliver_pending())
    assert "Body" not in captured[0]
    variables = json.loads(captured[0]["ContentVariables"][0])
    assert variables["3"] == "https://office.example/?request=req-1"
    assert "confirm" in variables["1"]


def test_endpoint_contract_is_available_with_transport_off(monkeypatch):
    monkeypatch.setenv("OFFICE_WHATSAPP_ENABLED", "false")
    with TestClient(create_app(":memory:")) as client:
        response = client.get("/api/feedback")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body["events"], list)
        assert body["channels"]["whatsapp"]["enabled"] is False
        assert body["channels"]["board"]["status"] == "unverified"


def test_concurrent_delivery_passes_send_each_pending_event_once(store):
    conn, _, _ = store
    captured = []
    async def accept(request):
        captured.append(parse_qs(request.content.decode())["Body"][0])
        await asyncio.sleep(0)
        return httpx.Response(201, json={"sid": "SM" + "1" * 32})
    feed = service(store, transport=httpx.MockTransport(accept))
    for task_id in ("req-one", "req-two", "req-three"):
        task(conn, task_id)
        approval(conn, task_id)
    feed.sync()
    async def deliver_concurrently():
        await asyncio.gather(feed.deliver_pending(), feed.deliver_pending())
    asyncio.run(deliver_concurrently())
    assert len(captured) == 3
    for task_id in ("req-one", "req-two", "req-three"):
        assert sum(f"?request={task_id}" in message for message in captured) == 1
    assert {event["whatsapp"]["status"] for event in feed.snapshot()["events"]} == {"sent"}


def test_recovery_is_captured_without_polling_and_supersedes_unsent_alert(store):
    conn, _, incidents = store
    captured = []
    def accept(request):
        captured.append(parse_qs(request.content.decode())["Body"][0])
        return httpx.Response(201, json={"sid": "SM" + "1" * 32})
    feed = service(store, transport=httpx.MockTransport(accept))
    feed.start()
    try:
        incidents.append({"id": "rapid-incident", "status": "active", "board_id": "kitchen", "title": "CO₂ high",
                          "opened_at": db.now(), "recovered_at": None, "severity": "urgent", "simulation": False,
                          "action_results": {}})
        bus.publish("facility.updated", {}, source=conn)
        assert {event["kind"] for event in feed.snapshot()["events"]} == {"alert"}
        incidents[0] |= {"status": "recovered", "recovered_at": db.now()}
        bus.publish("facility.updated", {}, source=conn)
        assert {event["kind"] for event in feed.snapshot()["events"]} == {"alert", "recovered"}
        asyncio.run(feed.deliver_pending())
        events = {event["kind"]: event for event in feed.snapshot()["events"]}
        assert events["alert"]["whatsapp"]["status"] == "skipped"
        assert "Facility state changed" in events["alert"]["whatsapp"]["detail"]
        assert events["recovered"]["whatsapp"]["status"] == "sent"
        asyncio.run(feed.deliver_pending())
        assert len(captured) == 1
        assert "back within the configured range" in captured[0]
    finally:
        feed.close()


def test_capture_lifecycle_is_source_isolated_and_close_stops_recording(store):
    conn, facilities, _ = store
    other_conn = db.connect(":memory:")
    procurement_workflow.ensure_schema(other_conn)
    feed = service(store)
    other_feed = FeedbackService(other_conn, facilities, asyncio.Lock(), settings())
    feed.start()
    feed.start()
    other_feed.start()
    try:
        task(conn)
        approval(conn)
        bus.publish("task.updated", {"id": "req-1"}, source=other_conn)
        assert feed.snapshot()["events"] == []
        bus.publish("task.updated", {"id": "req-1"}, source=conn)
        assert len(feed.snapshot()["events"]) == 1
        assert other_feed.snapshot()["events"] == []
        feed.close()
        feed.close()
        task(conn, "after-close")
        approval(conn, "after-close")
        bus.publish("requests.updated", {"request_id": "after-close"}, source=conn)
        assert [event["request_id"] for event in feed.snapshot()["events"]] == ["req-1"]
        task(other_conn, "other-app")
        approval(other_conn, "other-app")
        bus.publish("task.updated", {"id": "other-app"}, source=other_conn)
        assert [event["request_id"] for event in other_feed.snapshot()["events"]] == ["other-app"]
    finally:
        feed.close()
        other_feed.close()
        other_conn.close()


def test_restart_skips_never_attempted_pending_but_preserves_uncertain(store):
    conn, _, _ = store
    captured = []
    def accept(request):
        captured.append(request)
        return httpx.Response(201, json={"sid": "SM" + "1" * 32})
    feed = service(store, transport=httpx.MockTransport(accept))
    for task_id in ("queued", "attempted"):
        task(conn, task_id)
        approval(conn, task_id)
    feed.sync()
    attempted = next(event for event in feed.snapshot()["events"] if event["request_id"] == "attempted")
    feed._status(attempted["id"], "uncertain", "Attempt persisted before process interruption.")
    restarted = service(store, transport=httpx.MockTransport(accept))
    events = {event["request_id"]: event for event in restarted.snapshot()["events"]}
    assert events["queued"]["whatsapp"]["status"] == "skipped"
    assert "previous run" in events["queued"]["whatsapp"]["detail"]
    assert events["attempted"]["whatsapp"] == {
        "status": "uncertain", "detail": "Attempt persisted before process interruption."}
    asyncio.run(restarted.deliver_pending())
    assert captured == []


@pytest.mark.parametrize("resolution", ["native", "legacy"])
def test_api_captures_rapid_proposal_order_and_resolution_without_polling(monkeypatch, resolution):
    async def no_poll(self):
        await asyncio.Event().wait()
    monkeypatch.setattr(FeedbackService, "watch", no_poll)
    monkeypatch.setenv("OFFICE_WHATSAPP_ENABLED", "false")
    monkeypatch.setattr(config, "MODEL", "mock")
    monkeypatch.setitem(config.OFFICE, "require_approval", True)
    with TestClient(create_app(":memory:")) as client:
        result = client.post("/api/input", json={"board_id": "kitchen", "text": "We need coffee"})
        assert result.status_code == 200, result.text
        request = result.json()
        assert request["status"] == "awaiting_approval"
        task_id = request["id"]
        feed = client.app.state.feedback
        assert {event["kind"] for event in feed.snapshot()["events"] if event["request_id"] == task_id} == {"approval"}
        approved = client.post(f"/api/tasks/{task_id}/decide", json={
            "approve": True, "proposal_fingerprint": proposal_fingerprint(request["proposal"])})
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "done"
        assert {event["kind"] for event in feed.snapshot()["events"] if event["request_id"] == task_id} == {"approval", "ordered"}
        order = db.row(client.app.state.conn, "SELECT * FROM orders WHERE task_id=?", task_id)
        completed_at = "2026-11-05T23:59:59+00:00"
        monkeypatch.setattr(db, "now", lambda: completed_at)
        path = f"/api/orders/{order['id']}/delivered" if resolution == "native" else f"/api/requests/{task_id}/resolve"
        resolved = client.post(path, json={})
        assert resolved.status_code == 200, resolved.text
        events = [event for event in feed.snapshot()["events"] if event["request_id"] == task_id]
        assert {event["kind"] for event in events} == {"approval", "ordered", "resolved"}
        assert next(event for event in events if event["kind"] == "resolved")["created_at"] == completed_at
        assert client.post("/api/orders/unknown/delivered", json={}).status_code == 404
        assert client.post("/api/tickets/unknown/resolve", json={}).status_code == 404


def test_facility_routes_capture_alert_and_recovery_before_any_feedback_poll(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    async def no_poll(self):
        await asyncio.Event().wait()
    monkeypatch.setattr(FeedbackService, "watch", no_poll)
    monkeypatch.setenv("OFFICE_WHATSAPP_ENABLED", "false")
    with TestClient(create_app(":memory:")) as client:
        facilities = client.app.state.facilities
        clock = [datetime.now(UTC)]
        facilities.engine.clock = lambda: clock[0]
        rule = next(rule for rule in facilities.engine.snapshot()["rules"] if rule["id"] == "co2-high")
        configured = client.post("/api/facility/rules/co2-high", json={
            **rule, "min_samples": 1, "min_duration_seconds": 0, "actions": ["alert"]})
        assert configured.status_code == 200, configured.text
        for value in (1400, 900):
            clock[0] += timedelta(seconds=1)
            result = client.post("/api/facility/readings", json={
                "event_id": uuid4().hex, "board_id": "kitchen", "metric": "co2_ppm",
                "value": value, "timestamp": clock[0].isoformat(), "simulation": True})
            assert result.status_code == 200, result.text
        events = [event for event in client.app.state.feedback.snapshot()["events"] if event["title"] == rule["title"]]
        assert {event["kind"] for event in events} == {"alert", "recovered"}
        assert len(events) == 2
