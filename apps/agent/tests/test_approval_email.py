"""Approval-request emails: server-sent, once per exact proposal, never blocking the workflow. AgentMail is faked."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from agentmail.core.api_error import ApiError
from fastapi.testclient import TestClient

from pydantic_ai import DeferredToolRequests, ToolCallPart

from office import agent, approval_email, db, oda_food, procurement_workflow
from office.api import create_app
from office_procurement.serialization import proposal_fingerprint


class FakeMail:
    def __init__(self, error=None):
        self.sent, self.error = [], error
        self.inboxes = SimpleNamespace(messages=self)

    def send(self, inbox_id, **kwargs):
        self.sent.append({"inbox_id": inbox_id, **kwargs})
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=f"msg-{len(self.sent)}", thread_id="thread-1")


@pytest.fixture
def mail(monkeypatch):
    fake = FakeMail()
    monkeypatch.setattr(approval_email, "enabled", lambda: True)  # real gate: live model + APPROVAL_EMAIL_ENABLED
    monkeypatch.setattr(approval_email, "client", lambda: fake)
    monkeypatch.setattr(approval_email, "background", False)
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "office@agentmail.test")
    monkeypatch.setenv("APPROVER_EMAILS", "Noah:noah@example.com, Leo:leo@example.com")
    return fake


@pytest.fixture
def api():
    return TestClient(create_app(":memory:"))


def say(api, text, person=None, board="printer"):
    r = api.post("/api/input", json={"board_id": board, "text": text, "person": person})
    assert r.status_code == 200, r.text
    return r.json()


def emails(api, task_id):
    return db.rows(api.app.state.conn, "SELECT * FROM approval_emails WHERE task_id = ?", task_id)


def end_run(conn, task, output):
    """Fire the capability's after_run as the office agent run would, with `output` as the run's result."""
    ctx = SimpleNamespace(deps=SimpleNamespace(db=conn, task_id=task["id"]))
    result = SimpleNamespace(output=output)
    assert asyncio.run(approval_email.ApprovalEmail().after_run(ctx, result=result)) is result  # observe-only


def deferred(task):
    """The DeferredToolRequests a run ended with, rebuilt from the persisted pending calls."""
    calls = [ToolCallPart(p["tool"], p["args"], tool_call_id=p["tool_call_id"]) for p in task["pending"]]
    return DeferredToolRequests(approvals=calls, metadata={p["tool_call_id"]: p["details"] for p in task["pending"]})


def test_capability_is_on_the_agent_outermost_and_not_model_callable():
    capabilities = agent.office_agent.root_capability.capabilities
    assert isinstance(capabilities[0], approval_email.ApprovalEmail)  # last after_run: sees the final result
    cap = approval_email.ApprovalEmail()
    assert cap.get_toolset() is None and cap.get_instructions() is None and cap.id == "approval_email"


def test_capability_ignores_runs_that_do_not_await_a_human(api, mail):
    task = say(api, "we need 10 toner", person="Leo")
    conn = api.app.state.conn
    conn.execute("DELETE FROM approval_emails")
    end_run(conn, task, "Ordered (simulated).")
    end_run(conn, task, DeferredToolRequests())
    unrouted = deferred(task)
    unrouted.metadata = {}  # approval without application metadata: nothing exact to show
    end_run(conn, task, unrouted)
    assert len(mail.sent) == 1 and emails(api, task["id"]) == []


def test_over_cap_request_emails_the_routed_approver_once_with_exact_content(api, mail):
    task = say(api, "we need 10 toner", person="Leo")
    assert task["status"] == "awaiting_approval"
    assert len(mail.sent) == 1
    sent, details = mail.sent[0], task["pending"][0]["details"]
    assert sent["inbox_id"] == "office@agentmail.test" and sent["to"] == ["noah@example.com"]
    assert sent["request_options"] == {"max_retries": 0} and sent["idempotency_key"].startswith(f"approval-{task['id']}-")
    p = details["proposal"]
    item = f"{p['quantity']} × {p['product']['name']} ({p['product']['supplier']}) = {p['total_nok']:g} NOK"
    assert sent["subject"].startswith(f"[Simulated] Approval needed: {item}") and "printer" in sent["subject"]
    for expected in (item, f"Total: {p['total_nok']:g} NOK (simulated)", "Approval type: purchase_over_cap",
                     "Above the office cap", "Approver: Noah", "Leo (picked on the dashboard or device; unverified",
                     f"Task: {task['id']}", "Request text: we need 10 toner", "SIMULATED", "office dashboard",
                     "Replying to it does not approve or reject anything"):
        assert expected in sent["text"], expected
    [row] = emails(api, task["id"])
    assert row["state"] == "sent" and row["message_id"] == "msg-1" and row["approver"] == "Noah"
    assert "noah@example.com" not in str(row)  # the address stays in env, not the runtime DB


def test_retry_restart_and_rerun_do_not_resend(tmp_path, mail):
    path = str(tmp_path / "agent.sqlite")
    api = TestClient(create_app(path))
    task = say(api, "we need 10 toner", person="Leo")
    conn = api.app.state.conn
    approval_email.notify(conn, task["id"])  # transport retry
    end_run(conn, task, deferred(task))  # the same approval reached again by another run
    restarted = TestClient(create_app(path))  # restart recovers and sees the persisted send
    approval_email.notify(restarted.app.state.conn, task["id"])
    assert len(mail.sent) == 1 and len(emails(restarted, task["id"])) == 1


def test_refreshed_quote_with_new_fingerprint_can_email_again(api, mail):
    task = say(api, "we need 10 toner", person="Leo")
    old = proposal_fingerprint(task["proposal"])
    refreshed = api.post(f"/api/requests/{task['id']}/requote", json={"proposal_fingerprint": old})
    assert refreshed.status_code == 200
    assert proposal_fingerprint(refreshed.json()["proposal"]) != old
    approval_email.notify(api.app.state.conn, task["id"])
    assert len(mail.sent) == 2 and mail.sent[0]["idempotency_key"] != mail.sent[1]["idempotency_key"]
    assert [r["state"] for r in emails(api, task["id"])] == ["sent", "sent"]


@pytest.mark.parametrize("error, state", [
    (ApiError(status_code=500, body="boom"), "uncertain"),
    (httpx.ReadTimeout("read timed out"), "uncertain"),
    (ApiError(status_code=403, body="forbidden"), "failed"),
    (httpx.ConnectError("refused"), "failed"),
])
def test_send_failure_is_recorded_and_never_blocks_or_retries(api, mail, error, state):
    mail.error = error
    task = say(api, "we need 10 toner", person="Leo")
    assert task["status"] == "awaiting_approval" and task["pending"]
    approval_email.notify(api.app.state.conn, task["id"])
    assert len(mail.sent) == 1
    [row] = emails(api, task["id"])
    assert row["state"] == state and row["message_id"] is None
    done = api.post(f"/api/requests/{task['id']}/approve", json={"proposal_fingerprint": proposal_fingerprint(task["proposal"])})
    assert done.status_code == 200 and done.json()["status"] == "ordered"


def test_mock_mode_never_sends_even_when_enabled_in_env(api, monkeypatch):
    monkeypatch.setenv("APPROVAL_EMAIL_ENABLED", "true")
    assert approval_email.enabled() is False  # OFFICE_MODEL=mock in tests
    monkeypatch.setattr(approval_email, "client", lambda: pytest.fail("mock mode must not build a mail client"))
    task = say(api, "we need 10 toner", person="Leo")
    assert task["status"] == "awaiting_approval" and emails(api, task["id"]) == []


def test_disabled_sends_nothing(api, mail, monkeypatch):
    monkeypatch.setattr(approval_email, "enabled", lambda: False)
    task = say(api, "we need 10 toner", person="Leo")
    assert task["status"] == "awaiting_approval" and mail.sent == [] and emails(api, task["id"]) == []


def test_missing_config_is_recorded_as_skipped_until_fixed(api, mail, monkeypatch):
    conn = api.app.state.conn
    monkeypatch.setenv("APPROVER_EMAILS", "Leo:leo@example.com")  # nobody for Noah, the approver
    task = say(api, "we need 10 toner", person="Leo")
    assert task["status"] == "awaiting_approval" and mail.sent == []
    assert emails(api, task["id"])[0]["detail"] == "APPROVER_EMAILS has no address for Noah"

    monkeypatch.setenv("APPROVER_EMAILS", "Noah:noah@example.com")
    monkeypatch.delenv("AGENTMAIL_INBOX_ID")
    approval_email.notify(conn, task["id"])
    assert mail.sent == [] and emails(api, task["id"])[0]["detail"] == "AGENTMAIL_INBOX_ID is not set"

    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "office@agentmail.test")
    monkeypatch.setattr(approval_email, "client", lambda: None)
    approval_email.notify(conn, task["id"])
    assert mail.sent == [] and emails(api, task["id"])[0]["detail"] == "AGENTMAIL_API_KEY is not set"

    monkeypatch.setattr(approval_email, "client", lambda: mail)  # nothing was sent, so a fixed config may send once
    approval_email.notify(conn, task["id"])
    approval_email.notify(conn, task["id"])
    assert len(mail.sent) == 1 and [r["state"] for r in emails(api, task["id"])] == ["sent"]


def test_request_text_is_escaped_in_html_and_kept_out_of_the_subject(api, mail):
    task = say(api, "<script>alert('x')</script> & we need 10 toner", person="Leo")
    sent = mail.sent[0]
    assert "<script>" not in sent["html"] and "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; we need" in sent["html"]
    assert "script" not in sent["subject"]
    assert f"Request text: {task['prompt']}" in sent["text"]  # plain text part is not HTML


def test_sensor_replenishment_says_no_person_asked(api, mail):
    api.post("/api/sensor", json={"board_id": "bathroom", "kind": "stock_level", "value": 50})
    task = api.post("/api/sensor", json={"board_id": "bathroom", "kind": "stock_level", "value": 12}).json()
    assert task["status"] == "awaiting_approval"
    assert len(mail.sent) == 1 and "Requested by: Sensor rule (no person asked)" in mail.sent[0]["text"]


def test_oda_basket_email_does_not_claim_simulation(api, mail):
    conn = api.app.state.conn
    task_id = agent.start_task(conn, "kitchen", "Leo", "2 cartons of milk")
    basket = {"lines": [{"product_id": 7, "name": "Whole milk 1 l", "quantity": 2, "unit_price_nok": 25.9}],
              "subtotal_nok": 51.8, "expires_at": "2099-01-01T00:00:00.000Z"}
    value = {"source": "oda-food", "grocery": {"proposal": basket, "state": "awaiting_approval"}}
    oda_food.OdaFoodService(conn, client=object()).finish(task_id, value, "awaiting_approval", "Review the Oda basket.")
    [sent] = mail.sent
    assert "2 × Whole milk 1 l at 25.9 NOK" in sent["text"] and "connected Oda cart" in sent["text"]
    assert "SIMULATED" not in sent["text"]


def test_background_send_records_its_result(api, mail, monkeypatch):
    monkeypatch.setattr(approval_email, "enabled", lambda: False)
    task = say(api, "we need 10 toner", person="Leo")
    monkeypatch.setattr(approval_email, "enabled", lambda: True)
    monkeypatch.setattr(approval_email, "background", True)

    async def main():
        approval_email.notify(api.app.state.conn, task["id"])
        assert emails(api, task["id"])[0]["state"] == "sending"  # claimed before the network call
        await approval_email.drain()

    asyncio.run(main())
    assert len(mail.sent) == 1 and emails(api, task["id"])[0]["state"] == "sent"


def test_interrupted_send_becomes_uncertain_on_restart(tmp_path, mail, monkeypatch):
    path = str(tmp_path / "agent.sqlite")
    api = TestClient(create_app(path))
    record = approval_email._record
    monkeypatch.setattr(approval_email, "_record", lambda conn, job, outcome: None)  # process dies before recording
    task = say(api, "we need 10 toner", person="Leo")
    assert emails(api, task["id"])[0]["state"] == "sending"
    monkeypatch.setattr(approval_email, "_record", record)
    restarted = TestClient(create_app(path))
    assert emails(restarted, task["id"])[0]["state"] == "uncertain"
    approval_email.notify(restarted.app.state.conn, task["id"])
    assert len(mail.sent) == 1  # the possibly delivered email is never sent again
