"""Approval-request emails via AgentMail. A notification only: the decision is made on the dashboard.

Two server-side triggers, never a model tool:
- `ApprovalEmail`, a capability on the office agent: its `after_run` hook sees a run end with `DeferredToolRequests`
  (a tool raised `ApprovalRequired`) and emails from the approval metadata the application attached.
- `notify(conn, task_id)`, for paths that reach approval without an agent run (facility replenishment, Oda baskets,
  quote refresh): it reads the persisted task.
Both email the approver from the company brain's routing table, at most once per (task, exact proposal fingerprint),
recorded in `approval_emails` (office/db.py). A refreshed quote has a new fingerprint and may email again.

Replying to the email approves nothing: there is no authenticated inbound path. A send whose outcome is uncertain
is recorded as such and never retried automatically. Email failure or missing config never blocks the workflow.
Off unless APPROVAL_EMAIL_ENABLED=true with a live model; mock mode and tests never send.
"""

import asyncio
import hashlib
import html
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic_ai import DeferredToolRequests, RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.run import AgentRunResult

from office import brain, config, db, procurement_workflow as workflow, speaker
from office_procurement.serialization import proposal_fingerprint

log = logging.getLogger(__name__)
TIMEOUT_S = 15
background = True  # send off the event loop; tests set False to send inline
_inflight: set[asyncio.Future] = set()

SIMULATED = ("SIMULATED: approving records a simulated order. No real supplier order is placed; "
             "prices, delivery times and references are fixtures.")
ODA = "Approving adds these products to the connected Oda cart. It places and pays for nothing; that happens on Oda."


def enabled() -> bool:
    return config.LIVE and os.getenv("APPROVAL_EMAIL_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def recipients() -> dict[str, str]:
    """APPROVER_EMAILS="Leo:leo@example.com,Teammate 2:t2@example.com" → {approver name (lower-case): address}."""
    pairs = (entry.partition(":") for entry in os.getenv("APPROVER_EMAILS", "").split(","))
    return {name.strip().lower(): addr.strip() for name, _, addr in pairs if name.strip() and "@" in addr}


def client():
    from agentmail import AgentMail
    key = os.getenv("AGENTMAIL_API_KEY")
    return AgentMail(api_key=key, timeout=TIMEOUT_S) if key else None


def _requester(conn, task, annotation: dict | None) -> str:
    if task["person"] == "sensor":
        return "Sensor rule (no person asked)"
    a = annotation or speaker.load(conn, task["id"], task["person"]).annotation()
    how = {"voice_id": "voice ID" + (f", score {a['score']:.2f}" if a.get("score") is not None else ""),
           "declared": "picked on the dashboard or device"}.get(a.get("source"), "not identified")
    return f"{a.get('display_name') or 'unknown speaker'} ({how}; unverified, not authentication)"


def _approval(conn, task) -> dict | None:
    """What the dashboard asks a human to approve, read from the persisted task."""
    grocery = (workflow.get_context(conn, task["id"]) or {}).get("grocery") or {}
    if p := grocery.get("proposal"):
        return {"fingerprints": [proposal_fingerprint(p)], "approvers": [brain.approver("purchase")],
                "type": "purchase", "reason": "Exact Oda basket review before the cart is updated",
                "items": [f"{line['quantity']} × {line['name']} at {line['unit_price_nok']:g} NOK" for line in p["lines"]],
                "total": f"{p['subtotal_nok']:g} NOK in products (delivery fees are confirmed on Oda)",
                "effect": ODA, "requester": _requester(conn, task, None)}
    return _orders(conn, task, [c.get("details") or {} for c in json.loads(task["pending"])])


def _orders(conn, task, details: list[dict]) -> dict | None:
    """Supplier orders from `procurement_workflow.approval_metadata` (application code, never model output)."""
    calls = [d for d in details if d.get("proposal_fingerprint")]
    if not calls:
        return None
    items = [f"{d['proposal']['quantity']} × {d['proposal']['product']['name']} "
             f"({d['proposal']['product']['supplier']}) = {d['proposal']['total_nok']:g} NOK" for d in calls]
    return {"fingerprints": [d["proposal_fingerprint"] for d in calls], "approvers": [d["approver"] for d in calls],
            "type": ", ".join(dict.fromkeys(d["approval_type"] for d in calls)),
            "reason": "; ".join(dict.fromkeys(d["approval_reason"] for d in calls)),
            "items": items, "total": f"{sum(d['total_nok'] for d in calls):g} NOK (simulated)",
            "effect": SIMULATED, "requester": _requester(conn, task, calls[0].get("requester"))}


def message(task, a) -> tuple[str, str, str]:
    """(subject, text, html). Request text comes from people: it is escaped in HTML and kept out of the subject."""
    board = config.BOARDS.get(task["board_id"])
    where = f"{task['board_id']} ({board.location})" if board else task["board_id"]
    dashboard = os.getenv("DASHBOARD_URL", "http://localhost:5173")
    more = f" (+{len(a['items']) - 1} more)" if len(a["items"]) > 1 else ""
    label = "[Simulated] " if a["effect"] == SIMULATED else ""
    subject = " ".join(f"{label}Approval needed: {a['items'][0]}{more} · {where}".split())[:200]
    rows = [("Request", item) for item in a["items"]] + [
        ("Total", a["total"]), ("Approval type", a["type"]), ("Reason", a["reason"]),
        ("Approver", ", ".join(dict.fromkeys(a["approvers"]))), ("Requested by", a["requester"]),
        ("Board", where), ("Task", task["id"]), ("Request text", task["prompt"] or "(none)")]
    footer = [a["effect"], f"Decide on the office dashboard ({dashboard}): open task {task['id']}, then approve or reject it there.",
              "This email is a notification only. Replying to it does not approve or reject anything."]
    text = "\n".join(["An office request is waiting for your approval.", ""] + [f"{k}: {v}" for k, v in rows] + [""] + footer)
    cells = "".join(f"<tr><th align='left'>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    body = (f"<p>An office request is waiting for your approval.</p><table cellpadding='4'>{cells}</table>"
            + "".join(f"<p>{html.escape(line)}</p>" for line in footer))
    return subject, text, body


@dataclass
class ApprovalEmail(AbstractCapability[Any]):
    """Emails the routed approver when an office agent run ends waiting for a human.

    Observe-only: no tools, instructions or model settings, so the model can neither see nor trigger it, and it
    never resolves a deferred call (unlike a `handle_deferred_tool_calls` handler). `after_run` sees the final
    output; `outermost` makes it the last `after_run`, after any capability that changes the result. Stateless,
    so no `for_run`: the task and connection come from the run's deps (office/deps.py).
    """

    id: str | None = "approval_email"

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="outermost")

    async def after_run(self, ctx: RunContext[Any], *, result: AgentRunResult[Any]) -> AgentRunResult[Any]:
        if isinstance(out := result.output, DeferredToolRequests) and out.approvals:
            details = [out.metadata.get(call.tool_call_id, {}) for call in out.approvals]
            _notify(ctx.deps.db, ctx.deps.task_id, lambda task: _orders(ctx.deps.db, task, details))
        return result


def notify(conn, task_id: str) -> None:
    """Email the routed approver that persisted task `task_id` awaits approval (paths without an agent run)."""
    _notify(conn, task_id, lambda task: _approval(conn, task) if task["status"] == "awaiting_approval" else None)


def _notify(conn, task_id: str, approval) -> None:
    """Never raises and never blocks the workflow: failures are logged and recorded."""
    if not enabled():
        return
    try:
        task = db.row(conn, "SELECT * FROM tasks WHERE id = ?", task_id)
        job = _claim(conn, task, approval(task)) if task else None
    except Exception as error:
        log.warning("Approval email for task %s not prepared: %s", task_id, type(error).__name__)
        return
    if not job:
        return
    if background:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop:
            future = loop.run_in_executor(None, _send, job)
            _inflight.add(future)
            future.add_done_callback(_inflight.discard)
            # Done callbacks run on the loop thread, so the result is recorded between, not inside, transactions.
            future.add_done_callback(lambda f: _record(conn, job, ("uncertain", "cancelled", None) if f.cancelled() else f.result()))
            return
    _record(conn, job, _send(job))


def _claim(conn, task, a: dict | None) -> dict | None:
    """Persist the send before it happens; a row already present (other than `skipped`) means never send again."""
    if not a:
        return None
    task_id = task["id"]
    subject, text, body = message(task, a)
    key = hashlib.sha256("\n".join(a["fingerprints"]).encode()).hexdigest()
    idempotency_key = f"approval-{task_id}-{key[:40]}"  # AgentMail returns the original message for a repeated key
    approvers = list(dict.fromkeys(a["approvers"]))
    to = [addr for name in approvers if (addr := recipients().get(name.lower()))]
    inbox, mail = os.getenv("AGENTMAIL_INBOX_ID", "").strip(), client()
    skip = ("AGENTMAIL_INBOX_ID is not set" if not inbox else "AGENTMAIL_API_KEY is not set" if mail is None
            else f"APPROVER_EMAILS has no address for {', '.join(approvers)}" if not to else None)
    claimed = conn.execute(
        """INSERT INTO approval_emails VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
           ON CONFLICT(task_id, fingerprint) DO UPDATE SET approver = excluded.approver, state = excluded.state,
             detail = excluded.detail, updated_at = excluded.updated_at
           WHERE approval_emails.state = 'skipped'""",  # nothing was sent, so a fixed config may still send
        (task_id, key, ", ".join(approvers), "skipped" if skip else "sending", skip, idempotency_key, db.now(), db.now()),
    ).rowcount == 1
    if claimed and skip:
        log.warning("Approval email for task %s skipped: %s", task_id, skip)
    if not claimed or skip:
        return None
    return {"task_id": task_id, "key": key, "client": mail, "inbox": inbox, "to": to, "subject": subject,
            "text": text, "html": body, "idempotency_key": idempotency_key}


def _send(job) -> tuple[str, str | None, str | None]:
    """One request, no SDK retries: (state, detail, message_id). Only a definite refusal counts as `failed`."""
    from agentmail.core.api_error import ApiError
    try:
        sent = job["client"].inboxes.messages.send(
            job["inbox"], to=job["to"], subject=job["subject"], text=job["text"], html=job["html"],
            labels=["approval-request"], idempotency_key=job["idempotency_key"], request_options={"max_retries": 0})
        return "sent", None, sent.message_id
    except ApiError as error:  # a 5xx may have been accepted; any other status was refused
        code = error.status_code or 0
        return ("uncertain" if code >= 500 else "failed"), f"AgentMail HTTP {code}", None
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:  # never reached AgentMail
        return "failed", type(error).__name__, None
    except Exception as error:  # e.g. a read timeout after the request went out: do not guess
        return "uncertain", type(error).__name__, None


def _record(conn, job, outcome) -> None:
    state, detail, message_id = outcome
    conn.execute("""UPDATE approval_emails SET state = ?, detail = ?, message_id = ?, updated_at = ?
                    WHERE task_id = ? AND fingerprint = ? AND state = 'sending'""",
                 (state, detail, message_id, db.now(), job["task_id"], job["key"]))
    (log.info if state == "sent" else log.warning)("Approval email for task %s: %s%s", job["task_id"], state,
                                                     f" ({detail})" if detail else "")


def recover(conn) -> None:
    """At startup a send still `sending` was interrupted: its outcome is unknown, so it is never resent."""
    conn.execute("UPDATE approval_emails SET state = 'uncertain', detail = 'interrupted by restart', updated_at = ? "
                 "WHERE state = 'sending'", (db.now(),))


async def drain(timeout: float = TIMEOUT_S) -> None:
    """Wait for in-flight sends (shutdown, tests). Anything still running stays `sending` → `uncertain` on restart."""
    if _inflight:
        await asyncio.wait(list(_inflight), timeout=timeout)
        await asyncio.sleep(0)  # let the done callbacks record their results
