"""Durable operational notifications. Messages never approve or execute a purchase."""
import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException

from office import bus, db
from office import requests_api as legacy


@dataclass(frozen=True)
class WhatsApp:
    enabled: bool = False
    account_sid: str = ""
    token: str = ""
    sender: str = ""
    recipient: str = ""
    dashboard_url: str = "http://127.0.0.1:5173"
    content_sid: str = ""

    @classmethod
    def from_env(cls):
        return cls(os.getenv("OFFICE_WHATSAPP_ENABLED", "").lower() == "true",
                   os.getenv("TWILIO_ACCOUNT_SID", ""), os.getenv("TWILIO_AUTH_TOKEN", ""),
                   os.getenv("OFFICE_WHATSAPP_FROM", ""), os.getenv("OFFICE_WHATSAPP_TO", ""),
                   os.getenv("OFFICE_DASHBOARD_URL", "http://127.0.0.1:5173"),
                   os.getenv("OFFICE_WHATSAPP_CONTENT_SID", ""))

    @property
    def configured(self):
        try:
            url = urlsplit(self.dashboard_url)
            url.port
        except ValueError:
            return False
        safe_url = (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))
        return bool(re.fullmatch(r"AC[0-9a-fA-F]{32}", self.account_sid) and self.token and
                    re.fullmatch(r"whatsapp:\+[1-9][0-9]{6,14}", self.sender) and
                    re.fullmatch(r"whatsapp:\+[1-9][0-9]{6,14}", self.recipient) and
                    safe_url and url.hostname and not url.username and not url.password and
                    not url.query and not url.fragment and
                    (not self.content_sid or re.fullmatch(r"HX[0-9a-fA-F]{32}", self.content_sid)))

    def link(self, request_id):
        # Never carry arbitrary existing query strings, fragments or raw user text.
        parsed = urlsplit(self.dashboard_url)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/",
                           urlencode({"request": request_id}) if request_id else "", ""))

    def channel(self):
        if not self.enabled:
            detail = "WhatsApp is off. Preview messages here; no messages are sent."
        elif not self.configured:
            detail = "WhatsApp setup required: configure sender, recipient, credentials and dashboard URL."
        else:
            detail = "WhatsApp enabled for new updates. Provider acceptance does not confirm delivery."
        return {"enabled": self.enabled, "configured": self.configured, "detail": detail}


def _identity(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:32]


def request_event(request):
    status = request["status"]
    kinds = {"awaiting_input": ("input", "Ready to listen"),
             "awaiting_approval": ("approval", "Your confirmation is needed"),
             "ordered": ("ordered", "Order confirmed · simulated"),
             "maintenance_open": ("maintenance", "Maintenance task created"),
             "resolved": ("resolved", "Request resolved"),
             "failed": ("failed", "Request needs attention"),
             "needs_clarification": ("clarification", "A little more detail is needed")}
    if status not in kinds:
        return None
    kind, title = kinds[status]
    grocery = request.get("grocery") or {}
    proposal = request.get("proposal") or grocery.get("proposal")
    detail = {"input": "Describe what you need at this station.",
              "approval": "Review the exact proposal in the dashboard before confirming.",
              "ordered": "The supplier order is simulated; no real purchase was made.",
              "maintenance": "A local task was recorded; nobody has been contacted.",
              "resolved": "The request was marked resolved in the dashboard.",
              "failed": "Review the request in the dashboard. No automatic purchase retry.",
              "clarification": "Open the request to provide the missing information."}[kind]
    if grocery and kind == "approval":
        # The current main uses real Oda baskets, not simulated supplier orders.
        detail = "Review the exact Oda basket in the dashboard. Adding products to the cart does not place or pay an order."
    elif proposal and kind in {"approval", "ordered"}:
        detail = (f"{proposal['quantity']} × {proposal['product']['name']} · "
                  f"{proposal['total_nok']:.2f} NOK total. Simulated supplier. " + detail)
    return {"id": _identity("request", request["id"], status, proposal if kind == "approval" else None),
            "request_id": request["id"], "station_id": request["station_id"], "kind": kind,
            "title": title, "detail": detail, "created_at": request["updated_at"],
            "severity": (request.get("assessment") or {}).get("severity", "urgent" if kind == "failed" else "routine"),
            "simulation": request.get("simulation", not bool(grocery))}


class FeedbackService:
    def __init__(self, conn, facilities, lock, settings=None, transport=None):
        self.conn, self.facilities, self.lock = conn, facilities, lock
        self.settings = settings or WhatsApp.from_env()
        self.transport = transport
        self._unsubscribe = None
        conn.execute("CREATE TABLE IF NOT EXISTS feedback_events(id TEXT PRIMARY KEY, body TEXT NOT NULL, whatsapp_status TEXT NOT NULL, whatsapp_detail TEXT NOT NULL, provider_reference TEXT)")
        # Attempts are persisted as uncertain BEFORE HTTP; pending means never attempted.
        conn.execute("UPDATE feedback_events SET whatsapp_status='skipped',whatsapp_detail='Queued update from a previous run; no historical message was sent.' WHERE whatsapp_status='pending'")
        self.sync(historical=True)

    def start(self):
        if self._unsubscribe is None:
            self._unsubscribe = bus.add_listener(self._capture, source=self.conn)

    def close(self):
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _capture(self, name, data):
        if name not in {"task.updated", "requests.updated", "state.changed", "facility.updated"}:
            return
        task_id = data.get("request_id") or (data.get("id") if name == "task.updated" else None)
        if task_id:
            try:
                event = request_event(legacy.request_view(self.conn, task_id))
            except HTTPException as error:
                if error.status_code == 404:
                    return
                raise
            if event and self._record(event):
                bus.publish("feedback.updated", {})
        else:
            self.sync()

    def candidates(self):
        for task in db.rows(self.conn, "SELECT id FROM tasks ORDER BY rowid DESC LIMIT 200"):
            event = request_event(legacy.request_view(self.conn, task["id"]))
            if event:
                yield event
        yield from self.facility_candidates()

    def facility_candidates(self):
        for incident in self.facilities.engine.snapshot()["incidents"]:
            recovered = incident["status"] == "recovered"
            results = incident.get("action_results", {})
            linked = next((r.get("task_id") for r in results.values() if isinstance(r, dict) and r.get("task_id")), None)
            yield {"id": _identity("facility", incident["id"], incident["status"]),
                   "request_id": linked, "station_id": incident["board_id"],
                   "kind": "recovered" if recovered else "alert", "title": incident["title"],
                   "detail": ("Reading is back within the configured range." if recovered else
                              "A configured facility rule needs attention. Review the dashboard.") +
                             (" Simulated reading." if incident["simulation"] else " Physical sensor reading."),
                   "created_at": incident["recovered_at"] if recovered else incident["opened_at"],
                   "severity": incident["severity"], "simulation": incident["simulation"]}

    def _record(self, event, historical=False):
        if historical:
            status, detail = "skipped", "Existing update; no historical message was sent."
        elif not self.settings.enabled or not self.settings.configured:
            status, detail = "disabled", "WhatsApp is off or setup is incomplete. Preview only."
        elif event["kind"] == "input":
            status, detail = "skipped", "Listening prompts stay on the station and dashboard."
        else:
            status, detail = "pending", "Waiting to notify the configured recipient."
        return bool(self.conn.execute("INSERT OR IGNORE INTO feedback_events VALUES(?,?,?,?,NULL)",
                                     (event["id"], json.dumps(event), status, detail)).rowcount)

    def sync(self, historical=False):
        changed = False
        for event in self.candidates():
            changed = self._record(event, historical) or changed
        if changed:
            bus.publish("feedback.updated", {})

    def snapshot(self):
        events = []
        for row in db.rows(self.conn, "SELECT * FROM feedback_events ORDER BY json_extract(body,'$.created_at') DESC,rowid DESC LIMIT 30"):
            events.append(json.loads(row["body"]) | {"whatsapp": {"status": row["whatsapp_status"], "detail": row["whatsapp_detail"]}})
        return {"events": events, "channels": {"whatsapp": self.settings.channel(),
                "board": {"status": "unverified", "detail": "Board sound requires the physical voice-station bridge; audible output is not verified here."}}}

    def _status(self, event_id, status, detail, reference=None):
        self.conn.execute("UPDATE feedback_events SET whatsapp_status=?,whatsapp_detail=?,provider_reference=? WHERE id=?",
                          (status, detail, reference, event_id))
        bus.publish("feedback.updated", {})

    async def deliver_pending(self):
        if not self.settings.enabled or not self.settings.configured:
            return
        # Single watcher is the only sender; GET only synchronizes durable events.
        for row in db.rows(self.conn, "SELECT * FROM feedback_events WHERE whatsapp_status='pending' ORDER BY rowid LIMIT 10"):
            event = json.loads(row["body"])
            async with self.lock:
                # A previous pass can have delivered a row from this same snapshot.
                state = db.row(self.conn, "SELECT whatsapp_status FROM feedback_events WHERE id=?", event["id"])
                if not state or state["whatsapp_status"] != "pending":
                    continue
                if event["kind"] in {"approval", "clarification", "failed", "maintenance"}:
                    try:
                        current = legacy.request_view(self.conn, event["request_id"])
                    except HTTPException as error:
                        if error.status_code != 404:
                            raise
                        current = None
                    latest = request_event(current) if current else None
                    if not latest or latest["id"] != event["id"]:
                        subject = "Approval prompt" if event["kind"] == "approval" else "Update"
                        self._status(event["id"], "skipped", f"{subject} is stale or superseded; the request or exact proposal changed. No message was sent.")
                        continue
                elif event["kind"] in {"alert", "recovered"}:
                    current_ids = {item["id"] for item in self.facility_candidates()}
                    if event["id"] not in current_ids:
                        self._status(event["id"], "skipped", "Facility state changed before sending; no stale message was sent.")
                        continue
                # Compare-and-set makes concurrent workers claim each message once.
                claimed = self.conn.execute("UPDATE feedback_events SET whatsapp_status='uncertain',whatsapp_detail=? WHERE id=? AND whatsapp_status='pending'",
                    ("Send attempted; delivery unknown until provider response. Not automatically retried.", event["id"])).rowcount
                if not claimed:
                    continue
                bus.publish("feedback.updated", {})
            link = self.settings.link(event["request_id"])
            payload = {"From": self.settings.sender, "To": self.settings.recipient}
            if self.settings.content_sid:
                payload |= {"ContentSid": self.settings.content_sid,
                            "ContentVariables": json.dumps({"1": event["title"], "2": event["detail"], "3": link})}
            else:
                payload["Body"] = f"Office assistant · {event['station_id']}\n{event['title']}\n{event['detail']}\n{link}"
            # The mutation lock is released before HTTP; notifications never block approval.
            try:
                async with httpx.AsyncClient(timeout=8, transport=self.transport, follow_redirects=False) as client:
                    response = await client.post(f"https://api.twilio.com/2010-04-01/Accounts/{self.settings.account_sid}/Messages.json",
                                                 auth=(self.settings.account_sid, self.settings.token), data=payload)
                if 200 <= response.status_code < 300:
                    body = response.json()
                    reference = body.get("sid") if isinstance(body, dict) else None
                    if isinstance(body, dict) and body.get("status") in {"failed", "undelivered"}:
                        self._status(event["id"], "failed", "WhatsApp provider reported a failed message. No retry was made.", reference if isinstance(reference, str) else None)
                    elif isinstance(reference, str) and re.fullmatch(r"[MS]M[0-9a-fA-F]{32}", reference):
                        self._status(event["id"], "sent", "Accepted by WhatsApp provider; delivery is not confirmed.", reference)
                    else:
                        self._status(event["id"], "uncertain", "Provider response could not be verified. Not retried.")
                elif 400 <= response.status_code < 500:
                    self._status(event["id"], "failed", "WhatsApp provider rejected the message. Check provider configuration; no retry was made.")
                else:
                    self._status(event["id"], "uncertain", "Provider result is uncertain. Check provider logs; no retry was made.")
            except (httpx.HTTPError, ValueError):
                self._status(event["id"], "uncertain", "WhatsApp delivery is unknown after a network error. Check provider logs; no retry was made.")

    async def watch(self):
        while True:
            async with self.lock:
                self.sync()
            await self.deliver_pending()
            await asyncio.sleep(1)


def router(service):
    routes = APIRouter()

    @routes.get("/api/feedback")
    async def feedback():
        async with service.lock:
            service.sync()
            return service.snapshot()

    return routes
