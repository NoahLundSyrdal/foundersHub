"""The local WhatsApp bridge queue never confuses acceptance with delivery."""
import asyncio
import json
import sqlite3
import stat

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from office.whatsapp_delivery import Receipt, WhatsAppDelivery, router


@pytest.fixture
def bridge(tmp_path):
    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    now = [1000.0]
    token_path = tmp_path / "private" / "bridge-token"
    service = WhatsAppDelivery(conn, asyncio.Lock(), token_path, lambda: now[0])
    app = FastAPI()
    app.include_router(router(service))
    with TestClient(app) as client:
        yield service, client, now, token_path
    conn.close()


def headers(bridge):
    return {"Authorization": "Bearer " + bridge[3].read_text()}


def enqueue(bridge, execution_id="exec-1", message_id="message-1", phone="+12025550123", body="Synthetic message"):
    service = bridge[0]
    async def run():
        async with service.lock:
            return await service.enqueue(message_id, phone, body, execution_id)
    return asyncio.run(run())


def heartbeat(bridge, consumer="bridge-a", connected=True):
    return bridge[1].post("/api/whatsapp/heartbeat", headers=headers(bridge), json={"consumer_id": consumer, "connected": connected})


def claim(bridge, consumer="bridge-a"):
    return bridge[1].post("/api/whatsapp/jobs/claim", headers=headers(bridge), json={"consumer_id": consumer})


def receipt(bridge, status, consumer="bridge-a", execution_id="exec-1", **extra):
    return bridge[1].post(f"/api/whatsapp/jobs/{execution_id}/receipt", headers=headers(bridge),
                          json={"consumer_id": consumer, "status": status, **extra})


def test_success_receipt_requires_a_real_message_reference(bridge):
    enqueue(bridge)
    heartbeat(bridge)
    claim(bridge)
    assert receipt(bridge, "sent").status_code == 409
    assert receipt(bridge, "delivered").status_code == 409
    assert bridge[0]._job("exec-1")["state"] == "uncertain"


def test_interrupted_claim_updates_progress_after_timeout_without_requeue(bridge):
    updates = []
    bridge[0].on_update = updates.append
    enqueue(bridge)
    heartbeat(bridge)
    claim(bridge)
    assert updates[-1]["event"] == "claim"
    bridge[2][0] += 31
    bridge[0].tick()
    assert updates[-1]["event"] == "update"
    assert updates[-1]["state"] == "uncertain"
    assert "timeout" in updates[-1]["detail"]
    heartbeat(bridge)
    assert claim(bridge).json()["jobs"] == []
    assert receipt(bridge, "delivered", provider_reference="late-provider-receipt").json()["state"] == "delivered"


def test_private_token_creation_reuse_and_public_status_never_exposes_private_data(bridge):
    service, client, _, token_path = bridge
    token = token_path.read_text()
    assert len(token) >= 43
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
    WhatsAppDelivery(service.conn, service.lock, token_path)
    assert token_path.read_text() == token
    enqueue(bridge)
    heartbeat(bridge)
    body = client.get("/api/whatsapp/status").json()
    assert body == {"connected": True, "heartbeat_age_seconds": 0.0, "queued": 1, "uncertain": 0}
    encoded = json.dumps(body)
    assert all(private not in encoded for private in (token, "+12025550123", "Synthetic message", "bridge-a", "message-1"))


@pytest.mark.parametrize("path,payload", [
    ("/api/whatsapp/heartbeat", {"consumer_id": "bridge-a", "connected": True}),
    ("/api/whatsapp/jobs/claim", {"consumer_id": "bridge-a"}),
    ("/api/whatsapp/jobs/exec-1/receipt", {"consumer_id": "bridge-a", "status": "sent"}),
])
def test_all_mutating_endpoints_require_bridge_authorization(bridge, path, payload):
    client = bridge[1]
    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, headers={"Authorization": "Bearer wrong"}, json=payload).status_code == 401


def test_enqueue_is_honestly_queued_and_identifier_is_bound_to_exact_payload(bridge):
    assert enqueue(bridge) == {"status": "queued", "provider_reference": "exec-1"}
    assert enqueue(bridge) == {"status": "queued", "provider_reference": "exec-1"}
    assert bridge[0].conn.execute("SELECT COUNT(*) FROM whatsapp_delivery_jobs").fetchone()[0] == 1
    for changes in ({"body": "Changed"}, {"phone": "+12025550124"}, {"message_id": "different-message"}):
        with pytest.raises(HTTPException) as error:
            enqueue(bridge, **changes)
        assert error.value.status_code == 409


def test_claim_requires_matching_fresh_connected_consumer_and_returns_one_job(bridge):
    enqueue(bridge)
    enqueue(bridge, execution_id="exec-2", message_id="message-2")
    assert claim(bridge).status_code == 409
    assert heartbeat(bridge).status_code == 200
    assert claim(bridge, "bridge-b").status_code == 409
    first = claim(bridge)
    assert first.status_code == 200
    assert first.json() == {"jobs": [{"execution_id": "exec-1", "message_id": "message-1", "phone": "+12025550123", "body": "Synthetic message"}]}
    assert bridge[0].conn.execute("SELECT state,consumer_id FROM whatsapp_delivery_jobs WHERE execution_id='exec-1'").fetchone() == ("uncertain", "bridge-a")
    assert claim(bridge).json()["jobs"][0]["execution_id"] == "exec-2"
    assert claim(bridge).json() == {"jobs": []}


def test_disconnection_and_heartbeat_expiry_are_not_live_connections(bridge):
    enqueue(bridge)
    heartbeat(bridge)
    assert heartbeat(bridge, "bridge-b").status_code == 409
    assert heartbeat(bridge, connected=False).json() == {"connected": False}
    assert claim(bridge).status_code == 409
    heartbeat(bridge)
    bridge[2][0] += 15
    assert bridge[1].get("/api/whatsapp/status").json()["connected"] is False
    assert claim(bridge).status_code == 409
    assert heartbeat(bridge, "bridge-b").status_code == 200
    assert claim(bridge, "bridge-b").json()["jobs"][0]["execution_id"] == "exec-1"


def test_only_never_claimed_jobs_expire_and_expired_jobs_never_requeue(bridge):
    enqueue(bridge)
    enqueue(bridge, execution_id="exec-2", message_id="message-2")
    heartbeat(bridge)
    claim(bridge)
    bridge[2][0] += 120
    bridge[0].tick()
    rows = dict(bridge[0].conn.execute("SELECT execution_id,state FROM whatsapp_delivery_jobs").fetchall())
    assert rows == {"exec-1": "uncertain", "exec-2": "expired"}
    assert enqueue(bridge, execution_id="exec-2", message_id="message-2")["status"] == "expired"
    heartbeat(bridge)
    assert claim(bridge).json() == {"jobs": []}


def test_claim_skips_stale_context_before_releasing_private_payload(bridge):
    enqueue(bridge)
    enqueue(bridge, execution_id="exec-2", message_id="message-2")
    bridge[0].validate_job = lambda job: job["message_id"] == "message-2"
    heartbeat(bridge)
    assert claim(bridge).json()["jobs"][0]["execution_id"] == "exec-2"
    row = bridge[0].conn.execute("SELECT state,consumer_id FROM whatsapp_delivery_jobs WHERE execution_id='exec-1'").fetchone()
    assert row == ("skipped", None)


def test_receipt_progression_is_truthful_monotonic_and_duplicate_idempotent(bridge):
    updates = []
    bridge[0].on_update = updates.append
    enqueue(bridge)
    heartbeat(bridge)
    claim(bridge)
    assert receipt(bridge, "uncertain", detail="Awaiting acknowledgement").json()["state"] == "uncertain"
    assert receipt(bridge, "sent", provider_reference="provider-1").json()["state"] == "sent"
    count = len(updates)
    assert receipt(bridge, "sent", provider_reference="provider-1").status_code == 200
    assert len(updates) == count
    assert receipt(bridge, "uncertain").status_code == 409
    assert receipt(bridge, "failed").status_code == 409
    assert receipt(bridge, "delivered", provider_reference="provider-1").json()["state"] == "delivered"
    assert receipt(bridge, "sent", provider_reference="provider-1").status_code == 409
    final = receipt(bridge, "read", provider_reference="provider-1")
    assert final.json()["state"] == "read"
    assert receipt(bridge, "delivered").status_code == 409
    assert receipt(bridge, "read", provider_reference="provider-other").status_code == 409
    assert all(set(update) == {"message_id", "execution_id", "state", "provider_reference", "detail", "event"} for update in updates)
    assert all("+12025550123" not in json.dumps(update) and "Synthetic message" not in json.dumps(update) for update in updates)


def test_receipts_require_claim_owner_even_after_another_bridge_connects(bridge):
    enqueue(bridge)
    heartbeat(bridge)
    claim(bridge)
    assert receipt(bridge, "sent", consumer="bridge-b").status_code == 409
    assert receipt(bridge, "sent", execution_id="unknown").status_code == 404
    bridge[2][0] += 16
    heartbeat(bridge, "bridge-b")
    assert receipt(bridge, "delivered", consumer="bridge-a", provider_reference="provider-1").status_code == 200
    assert receipt(bridge, "read", consumer="bridge-b", provider_reference="provider-1").status_code == 409


def test_failure_is_not_automatically_retried(bridge):
    enqueue(bridge)
    heartbeat(bridge)
    claim(bridge)
    assert receipt(bridge, "failed").json()["state"] == "failed"
    assert receipt(bridge, "failed").status_code == 200
    assert receipt(bridge, "sent").status_code == 409
    assert claim(bridge).json() == {"jobs": []}


def test_claimed_jobs_remain_uncertain_across_service_restart(bridge):
    enqueue(bridge)
    heartbeat(bridge)
    claim(bridge)
    original = bridge[0]
    restarted = WhatsAppDelivery(original.conn, original.lock, bridge[3], lambda: bridge[2][0])
    assert restarted.claim("bridge-a") == {"jobs": []}
    assert restarted.status()["uncertain"] == 1
    assert restarted.receipt("exec-1", Receipt(consumer_id="bridge-a", status="delivered", provider_reference="actual-1"))["state"] == "delivered"


def test_queues_and_tokens_are_isolated_between_application_instances(bridge, tmp_path):
    enqueue(bridge)
    other_conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        other_path = tmp_path / "other-private" / "token"
        other = WhatsAppDelivery(other_conn, asyncio.Lock(), other_path, lambda: 1000)
        assert other.status() == {"connected": False, "heartbeat_age_seconds": None, "queued": 0, "uncertain": 0}
        assert other_path.read_text() != bridge[3].read_text()
        with pytest.raises(HTTPException) as error:
            other.authorize(headers(bridge)["Authorization"])
        assert error.value.status_code == 401
        assert bridge[0].status()["queued"] == 1
    finally:
        other_conn.close()


def test_update_callback_runs_with_router_or_enqueue_lock_and_cannot_break_queue(bridge, caplog):
    service = bridge[0]
    held = []
    def failing_callback(job):
        held.append(service.lock.locked())
        raise RuntimeError("private phone and body should not be logged")
    service.on_update = failing_callback
    enqueue(bridge)
    heartbeat(bridge)
    assert claim(bridge).status_code == 200
    assert held == [True, True]
    assert "private phone" not in caplog.text
    assert "queue state was preserved" in caplog.text
