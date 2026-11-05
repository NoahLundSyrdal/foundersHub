"""Room-owner messaging binds generated content to verified booking context."""
import asyncio
import json
import sqlite3

import pytest

from office.room_messaging import DraftDecision, RoomMessageConflict, RoomMessageWorkflow


@pytest.fixture
def source():
    booking = {"id": "booking-1", "room_name": "Library", "starts_at": "2026-11-05T13:00:00+01:00",
               "ends_at": "2026-11-05T14:00:00+01:00", "owner_id": "owner-1", "revision": 1, "simulation": True}
    contacts = {"owner-1": {"id": "owner-1", "name": "Jürgen", "whatsapp_number": "+12025550123"},
                "owner-2": {"id": "owner-2", "name": "Taylor", "whatsapp_number": "+12025550124"}}
    conn = sqlite3.connect(":memory:", isolation_level=None)
    yield conn, booking, contacts
    conn.close()


def workflow(source, compose, sender=None):
    conn, booking, contacts = source
    return RoomMessageWorkflow(conn, lambda booking_id: booking if booking_id == booking["id"] else None,
                               contacts.get, compose, sender)


async def compose_message(context):
    return DraftDecision(action="message_owner", body=f"Hi {context['owner']['name']}, {context['requester_name']} asks: {context['instruction']}",
                         reply="A message is ready for review.")


def opened(flow):
    return flow.open_conflict("open-1", "booking-1", "Alex")


def drafted(flow, conversation_id, event_id="follow-1", text="Send him a message"):
    return asyncio.run(flow.follow_up(conversation_id, event_id, text))


def test_generated_follow_up_receives_trusted_context_without_phone_and_resolves_him(source):
    seen, sent = [], []
    async def compose(context):
        seen.append(context)
        return {"action": "message_owner", "reply": "Ready for your review.",
                "body": f"Hello {context['owner']['name']}, {context['requester_name']} needs {context['booking']['room_name']} at {context['booking']['starts_at']}. Could you help?"}
    async def sender(phone, body, execution_id):
        sent.append((phone, body, execution_id))
        return {"provider_reference": "provider-test-1", "status": "accepted"}
    flow = workflow(source, compose, sender)
    result = drafted(flow, opened(flow))
    assert seen[0] == {
        "instruction": "Send him a message", "requester_name": "Alex",
        "booking": source[1], "owner": {"id": "owner-1", "name": "Jürgen"}}
    assert "whatsapp_number" not in json.dumps(seen)
    assert "+12025550123" not in json.dumps(seen)
    assert result["status"] == "drafted" and result["recipient_id"] == "owner-1"
    assert result["body"].startswith("Office assistant test (simulated booking)\nHello Jürgen, Alex needs Library")
    assert sent == []
    dispatched = asyncio.run(flow.dispatch(result["id"], result["fingerprint"]))
    assert dispatched["status"] == "sent"
    assert sent == [("+12025550123", result["body"], result["id"])]


def test_different_instructions_generate_callback_specific_content(source):
    seen = []
    async def compose(context):
        seen.append(context["instruction"])
        return await compose_message(context)
    flow = workflow(source, compose)
    conversation_id = opened(flow)
    first = drafted(flow, conversation_id, "first", "Ask to swap our room booking")
    second = drafted(flow, conversation_id, "second", "Ask whether the room will be free fifteen minutes earlier")
    assert first["body"] != second["body"]
    assert "swap our room booking" in first["body"]
    assert "fifteen minutes earlier" in second["body"]
    assert len(seen) == 2


def test_same_event_reuses_persisted_draft_without_second_composition(source):
    calls = []
    async def compose(context):
        calls.append(context)
        return await compose_message(context)
    flow = workflow(source, compose)
    conversation_id = opened(flow)
    assert opened(flow) == conversation_id
    first = drafted(flow, conversation_id)
    assert drafted(flow, conversation_id) == first
    restarted = workflow(source, compose)
    assert drafted(restarted, conversation_id) == first
    assert len(calls) == 1
    assert source[0].execute("SELECT COUNT(*) FROM room_message_drafts").fetchone()[0] == 1
    with pytest.raises(RoomMessageConflict, match="different input"):
        drafted(restarted, conversation_id, text="Send a different message")
    with pytest.raises(RoomMessageConflict, match="different input"):
        flow.open_conflict("open-1", "booking-1", "Another requester")
    with pytest.raises(RoomMessageConflict, match="different input"):
        drafted(flow, conversation_id, "open-1")


def test_composer_failure_is_persisted_and_never_automatically_retried(source):
    calls = []
    async def fail(context):
        calls.append(context)
        raise RuntimeError("private model/provider error")
    flow = workflow(source, fail)
    conversation_id = opened(flow)
    result = drafted(flow, conversation_id)
    assert result["status"] == "failed"
    assert "private" not in json.dumps(result)
    assert drafted(workflow(source, fail), conversation_id) == result
    assert len(calls) == 1
    assert source[0].execute("SELECT COUNT(*) FROM room_message_drafts").fetchone()[0] == 0


def mutate(source, change):
    _, booking, contacts = source
    if change == "owner":
        booking["owner_id"] = "owner-2"
    elif change == "revision":
        booking["revision"] = 2
    elif change == "time":
        booking["ends_at"] = "2026-11-05T14:30:00+01:00"
    elif change == "phone":
        contacts["owner-1"]["whatsapp_number"] = "+12025550125"
    else:
        contacts["owner-1"]["name"] = "Renamed owner"


@pytest.mark.parametrize("change", ["owner", "revision", "time", "phone", "name"])
def test_changed_booking_or_contact_during_composition_cannot_save_draft(source, change):
    async def compose(context):
        mutate(source, change)
        return await compose_message(context)
    flow = workflow(source, compose)
    conversation_id = opened(flow)
    with pytest.raises(RoomMessageConflict, match="changed"):
        drafted(flow, conversation_id)
    assert source[0].execute("SELECT COUNT(*) FROM room_message_drafts").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["owner", "revision", "time", "phone", "name"])
def test_changed_booking_or_contact_before_dispatch_cannot_choose_wrong_recipient(source, change):
    calls = []
    async def sender(phone, body, execution_id):
        calls.append((phone, body, execution_id))
        return {"provider_reference": "provider-test-1", "status": "accepted"}
    flow = workflow(source, compose_message, sender)
    result = drafted(flow, opened(flow))
    mutate(source, change)
    with pytest.raises(RoomMessageConflict, match="changed"):
        asyncio.run(flow.dispatch(result["id"], result["fingerprint"]))
    assert calls == []
    assert source[0].execute("SELECT status FROM room_message_drafts").fetchone()[0] == "drafted"


def test_concurrent_dispatch_claims_execution_once_before_network(source):
    calls = []
    async def sender(phone, body, execution_id):
        assert source[0].execute("SELECT status FROM room_message_drafts WHERE id=?", (execution_id,)).fetchone()[0] == "uncertain"
        calls.append((phone, body, execution_id))
        await asyncio.sleep(0)
        return {"provider_reference": "provider-test-1", "status": "accepted"}
    flow = workflow(source, compose_message, sender)
    result = drafted(flow, opened(flow))
    async def dispatch_twice():
        return await asyncio.gather(flow.dispatch(result["id"], result["fingerprint"]),
                                    flow.dispatch(result["id"], result["fingerprint"]))
    outcomes = asyncio.run(dispatch_twice())
    assert len(calls) == 1
    assert {outcome["status"] for outcome in outcomes} == {"sent", "uncertain"}
    assert asyncio.run(flow.dispatch(result["id"], result["fingerprint"]))["status"] == "sent"
    assert len(calls) == 1


def test_ambiguous_send_is_not_retried_after_database_reopen(source, tmp_path):
    _, booking, contacts = source
    path = tmp_path / "room-messaging.sqlite"
    calls = []
    async def sender(phone, body, execution_id):
        calls.append((phone, body, execution_id))
        raise TimeoutError("private transport error")
    conn = sqlite3.connect(path, isolation_level=None)
    flow = workflow((conn, booking, contacts), compose_message, sender)
    result = drafted(flow, opened(flow))
    first = asyncio.run(flow.dispatch(result["id"], result["fingerprint"]))
    assert first["status"] == "uncertain" and "private" not in json.dumps(first)
    conn.close()
    reopened = sqlite3.connect(path, isolation_level=None)
    try:
        restarted = workflow((reopened, booking, contacts), compose_message, sender)
        assert asyncio.run(restarted.dispatch(result["id"], result["fingerprint"])) == first
        assert len(calls) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("provider_status,expected", [("queued", "queued"), ("accepted", "sent"), ("delivered", "delivered")])
def test_provider_acceptance_and_confirmed_delivery_are_distinct(source, provider_status, expected):
    calls = []
    async def sender(phone, body, execution_id):
        calls.append(execution_id)
        return {"provider_reference": "provider-test-1", "status": provider_status}
    flow = workflow(source, compose_message, sender)
    result = drafted(flow, opened(flow))
    sent = asyncio.run(flow.dispatch(result["id"], result["fingerprint"]))
    assert sent["status"] == expected and sent["provider_reference"] == "provider-test-1"
    assert asyncio.run(flow.dispatch(result["id"], result["fingerprint"])) == sent
    assert len(calls) == 1


@pytest.mark.parametrize("provider_result", [{"provider_reference": "ref", "status": "unknown"},
                                            {"status": "delivered"},
                                            {"provider_reference": " ", "status": "delivered"}, None])
def test_unverified_provider_result_stays_uncertain(source, provider_result):
    async def sender(phone, body, execution_id):
        return provider_result
    flow = workflow(source, compose_message, sender)
    result = drafted(flow, opened(flow))
    assert asyncio.run(flow.dispatch(result["id"], result["fingerprint"]))["status"] == "uncertain"


def test_fingerprint_mismatch_never_calls_sender(source):
    calls = []
    async def sender(phone, body, execution_id):
        calls.append(phone)
        return {"provider_reference": "provider-test-1", "status": "accepted"}
    flow = workflow(source, compose_message, sender)
    result = drafted(flow, opened(flow))
    with pytest.raises(RoomMessageConflict, match="fingerprint"):
        asyncio.run(flow.dispatch(result["id"], "changed-snapshot"))
    assert calls == []


def test_missing_phone_allows_draft_but_blocks_dispatch(source):
    source[2]["owner-1"]["whatsapp_number"] = None
    calls = []
    async def sender(phone, body, execution_id):
        calls.append(phone)
        return {"provider_reference": "provider-test-1", "status": "accepted"}
    flow = workflow(source, compose_message, sender)
    result = drafted(flow, opened(flow))
    assert result["status"] == "drafted"
    with pytest.raises(RoomMessageConflict, match="not configured"):
        asyncio.run(flow.dispatch(result["id"], result["fingerprint"]))
    assert calls == []


@pytest.mark.parametrize("action,expected", [("clarify", "clarification"), ("cancel", "cancelled")])
def test_clarification_or_cancellation_never_creates_outbound_draft(source, action, expected):
    async def compose(context):
        return {"action": action, "reply": "Please clarify." if action == "clarify" else "Cancelled."}
    flow = workflow(source, compose)
    result = drafted(flow, opened(flow))
    assert result["status"] == expected
    assert "body" not in result and "fingerprint" not in result
    assert source[0].execute("SELECT COUNT(*) FROM room_message_drafts").fetchone()[0] == 0


def test_model_cannot_choose_recipient_or_phone(source):
    async def compose(context):
        return {"action": "message_owner", "reply": "Ready.", "body": "A message", "recipient_id": "owner-2", "phone": "+12025550124"}
    flow = workflow(source, compose)
    assert drafted(flow, opened(flow))["status"] == "failed"
    assert source[0].execute("SELECT COUNT(*) FROM room_message_drafts").fetchone()[0] == 0


def test_booking_contact_owner_mismatch_and_ambiguous_lookup_are_rejected(source):
    conn, booking, contacts = source
    mismatched = RoomMessageWorkflow(conn, lambda _: booking, lambda _: contacts["owner-2"], compose_message)
    with pytest.raises(RoomMessageConflict, match="verified booking owner"):
        opened(mismatched)
    ambiguous = RoomMessageWorkflow(conn, lambda _: [booking, booking], contacts.get, compose_message)
    with pytest.raises(RoomMessageConflict, match="ambiguous"):
        opened(ambiguous)
