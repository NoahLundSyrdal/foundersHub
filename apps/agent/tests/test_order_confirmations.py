import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from office import agent, db, procurement_workflow as workflow
from office.order_confirmations import OrderConfirmations, ConfirmationTokenFilter, router


class Delivery:
    def __init__(self):
        self.jobs = []

    async def enqueue(self, message_id, phone, body, execution_id):
        self.jobs.append({"message_id": message_id, "phone": phone, "body": body, "execution_id": execution_id})
        return {"status": "queued", "provider_reference": execution_id}


def expiry(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def seed(conn, now=1000, *, real=False, name="Kaffe <script>unsafe</script>"):
    task_id = agent.start_task(conn, "kitchen", "unknown speaker", "Restock coffee")
    conn.execute("UPDATE tasks SET status='awaiting_approval' WHERE id=?", (task_id,))
    assessment = {"severity": "routine", "confidence": .95, "requires_confirmation": True, "reason": "Review exact basket."}
    proposal = {"kind": "purchase", "quantity": 2, "product": {"id": "coffee", "name": name, "price_nok": 100},
                "total_nok": 210, "currency": "NOK", "simulation": True, "expires_at": expiry(now + 600)}
    value = {"interpretation": {"intent": "purchase", "item_id": "coffee"}, "proposal": proposal,
             "assessment": assessment, "research": [], "approval_required": True}
    if real:
        proposal = {"kind": "grocery", "lines": [{"product_id": 11, "name": "Melk", "quantity": 2, "unit_price_nok": 22.9}],
                    "subtotal_nok": 45.8, "currency": "NOK", "expires_at": expiry(now + 600)}
        value.update(source="oda-food", proposal=None, grocery={"state": "awaiting_approval", "proposal": proposal, "cart_url": None})
    workflow.store_context(conn, task_id, value)
    return task_id, value


@pytest.fixture
def setup():
    conn = db.connect(":memory:")
    delivery, clock = Delivery(), [1000.0]
    service = OrderConfirmations(conn, asyncio.Lock(), delivery, recipient="+4799999999", public_base_url="https://office.example", clock=lambda: clock[0])
    app = FastAPI()
    app.include_router(router(service))
    yield conn, delivery, clock, service, app
    conn.close()


def token(delivery):
    return delivery.jobs[-1]["body"].rsplit("/confirm/", 1)[1]


@pytest.mark.anyio
async def test_single_job_exact_snapshot_token_hash_only_and_escaped_get(setup, monkeypatch):
    conn, delivery, clock, service, app = setup
    task_id, _ = seed(conn)
    one = await service.queue_request(task_id)
    two = await service.queue_request(task_id)
    assert one == two and len(delivery.jobs) == 1
    raw = token(delivery)
    assert raw not in json.dumps(one)
    stored = db.row(conn, "SELECT * FROM order_confirmations")
    assert stored["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in json.dumps(stored)
    assert "SIMULERT" in delivery.jobs[0]["body"] and "210,00 kr" in delivery.jobs[0]["body"]
    assert service.validate_job(delivery.jobs[0])
    decide = AsyncMock(side_effect=AssertionError("GET must not decide"))
    monkeypatch.setattr(agent, "decide", decide)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://office.example") as client:
        get = await client.get("/confirm/" + raw)
        head = await client.head("/confirm/" + raw)
    assert get.status_code == head.status_code == 200
    assert "<script>unsafe</script>" not in get.text
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in get.text
    assert get.headers["cache-control"] == "no-store"
    assert db.row(conn, "SELECT state FROM order_confirmations")["state"] == "pending"
    decide.assert_not_called()


@pytest.mark.anyio
async def test_stale_job_and_stale_reject_never_approve(setup, monkeypatch):
    conn, delivery, _, service, app = setup
    task_id, value = seed(conn)
    await service.queue_request(task_id)
    raw = token(delivery)
    value["proposal"]["total_nok"] = 999
    workflow.store_context(conn, task_id, value)
    assert not service.validate_job(delivery.jobs[0])
    decide = AsyncMock()
    monkeypatch.setattr(agent, "decide", decide)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://office.example") as client:
        assert (await client.get("/confirm/" + raw)).status_code == 409
        assert (await client.post("/confirm/" + raw + "/reject")).status_code == 409
    decide.assert_not_called()


@pytest.mark.anyio
async def test_expiry_and_bad_token_fail_closed(setup, monkeypatch):
    conn, delivery, clock, service, app = setup
    task_id, _ = seed(conn)
    await service.queue_request(task_id)
    raw = token(delivery)
    clock[0] += 601
    assert not service.validate_job(delivery.jobs[0])
    decide = AsyncMock()
    monkeypatch.setattr(agent, "decide", decide)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://office.example") as client:
        assert (await client.post("/confirm/" + raw)).status_code == 409
        assert (await client.get("/confirm/" + "a" * 43)).status_code == 404
    decide.assert_not_called()


@pytest.mark.anyio
async def test_exact_post_and_concurrent_replay_execute_once(setup, monkeypatch):
    conn, delivery, _, service, app = setup
    task_id, value = seed(conn, real=True)
    await service.queue_request(task_id)
    raw = token(delivery)
    async def decide(connection, received_id, approve, fingerprint):
        assert connection is conn and received_id == task_id and approve is True
        assert fingerprint == db.row(conn, "SELECT fingerprint FROM order_confirmations")["fingerprint"]
        value["grocery"].update(state="cart_ready", cart_url="https://oda.com/no/cart/")
        workflow.store_context(conn, task_id, value)
        conn.execute("UPDATE tasks SET status='checkout_ready',reply='Ingen ordre er betalt.' WHERE id=?", (task_id,))
        return workflow.enrich_task(conn, db.task_view(db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)))
    call = AsyncMock(side_effect=decide)
    monkeypatch.setattr(agent, "decide", call)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://office.example") as client:
        replies = await asyncio.gather(client.post("/confirm/" + raw), client.post("/confirm/" + raw))
    assert all(response.status_code == 200 for response in replies)
    assert "Se levering og betal hos Oda" in replies[0].text
    assert 'href="https://oda.com/no/cart/"' in replies[0].text
    assert "ikke en betalt ordre" in replies[0].text
    call.assert_awaited_once()
    assert not service.validate_job(delivery.jobs[0])


@pytest.mark.anyio
async def test_uncertain_attempt_and_failed_confirmation_never_retry(setup, monkeypatch):
    conn, delivery, _, service, app = setup
    task_id, _ = seed(conn)
    await service.queue_request(task_id)
    raw = token(delivery)
    async def uncertain(*args):
        conn.execute("INSERT INTO procurement_attempts VALUES(?,?,?,'uncertain',NULL)", (task_id, "test-tool", "snapshot"))
        raise RuntimeError("Provider failed after dispatch")
    decide = AsyncMock(side_effect=uncertain)
    monkeypatch.setattr(agent, "decide", decide)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://office.example") as client:
        assert (await client.post("/confirm/" + raw)).status_code == 409
        replay = await client.post("/confirm/" + raw)
        assert replay.status_code == 200 and "prøver ikke" in replay.text
    decide.assert_awaited_once()
    assert not service.validate_job(delivery.jobs[0])


@pytest.mark.anyio
async def test_external_attempt_before_click_blocks_confirmation(setup, monkeypatch):
    conn, delivery, _, service, app = setup
    task_id, _ = seed(conn)
    await service.queue_request(task_id)
    conn.execute("INSERT INTO procurement_attempts VALUES(?,?,?,'started',NULL)", (task_id, "test-tool", "snapshot"))
    decide = AsyncMock()
    monkeypatch.setattr(agent, "decide", decide)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://office.example") as client:
        assert (await client.post("/confirm/" + token(delivery))).status_code == 409
    decide.assert_not_called()


@pytest.mark.anyio
async def test_reject_consumes_link_without_reapproving_released_quote(setup, monkeypatch):
    conn, delivery, _, service, app = setup
    task_id, _ = seed(conn)
    await service.queue_request(task_id)
    raw = token(delivery)
    async def reject(connection, received_id, approve, fingerprint):
        assert approve is False
        workflow.release_quote(conn, task_id)
        conn.execute("UPDATE tasks SET status='done',reply='Avslått' WHERE id=?", (task_id,))
        return {"status": "done"}
    decide = AsyncMock(side_effect=reject)
    monkeypatch.setattr(agent, "decide", decide)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://office.example") as client:
        assert (await client.post("/confirm/" + raw + "/reject")).status_code == 200
        assert (await client.post("/confirm/" + raw)).status_code == 200
    decide.assert_awaited_once()


@pytest.mark.anyio
async def test_watch_only_queues_tasks_created_after_startup_and_cleans_up(setup):
    conn, delivery, _, _, _ = setup
    historical, _ = seed(conn)
    service = OrderConfirmations(conn, asyncio.Lock(), delivery, recipient="+4799999999", public_base_url="https://office.example", clock=lambda: 1000)
    fresh, _ = seed(conn)
    watcher = asyncio.create_task(service.watch())
    for _ in range(20):
        if delivery.jobs:
            break
        await asyncio.sleep(0)
    watcher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watcher
    assert len(delivery.jobs) == 1
    rows = db.rows(conn, "SELECT task_id FROM order_confirmations")
    assert rows == [{"task_id": fresh}] and fresh != historical


def test_access_log_filter_redacts_capability_in_formatted_request():
    from uvicorn.logging import AccessFormatter
    raw = "a" * 43
    record = logging.LogRecord("uvicorn.access", logging.INFO, "file", 1, '%s - "%s %s HTTP/%s" %d',
                               ("client", "POST", "/confirm/" + raw + "/reject", "1.1", 200), None)
    assert ConfirmationTokenFilter().filter(record)
    assert raw not in record.getMessage()
    assert "/confirm/[redacted]/reject" in record.getMessage()
    formatted = AccessFormatter('%(request_line)s %(status_code)s', use_colors=False).format(record)
    assert raw not in formatted and "200" in formatted
