"""Local authenticated bridge queue. A claimed job is never automatically retried."""
import asyncio
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import time
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from office.config import ROOT

_logger = logging.getLogger(__name__)
CLAIM_DETAIL = "Claimed by bridge; delivery is uncertain until a receipt arrives."


class Consumer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    consumer_id: str = Field(min_length=1, max_length=128, pattern=r"\S")


class Heartbeat(Consumer):
    connected: bool


class Receipt(Consumer):
    status: Literal["sent", "delivered", "read", "failed", "uncertain"]
    provider_reference: str | None = Field(default=None, min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=500)


def _token_file(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Bridge token must be a regular private file")
        path.chmod(0o600)
        token = path.read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise RuntimeError("Bridge token file is invalid")
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w", encoding="ascii") as output:
        output.write(token)
    return token


class WhatsAppDelivery:
    def __init__(self, conn, lock, token_path=ROOT / ".data/whatsapp/bridge-token", clock=time.time):
        self.conn, self.lock, self.clock = conn, lock, clock
        self._token = _token_file(token_path)
        self.on_update = None
        self.validate_job = None
        conn.execute("CREATE TABLE IF NOT EXISTS whatsapp_delivery_jobs(execution_id TEXT PRIMARY KEY,message_id TEXT NOT NULL,phone TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL,consumer_id TEXT,provider_reference TEXT,detail TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS whatsapp_delivery_heartbeat(id INTEGER PRIMARY KEY CHECK(id=1),consumer_id TEXT NOT NULL,connected INTEGER NOT NULL,at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS whatsapp_delivery_attempts(execution_id TEXT PRIMARY KEY,claimed_at REAL NOT NULL)")

    def authorize(self, authorization):
        supplied = authorization[7:] if isinstance(authorization, str) and authorization.startswith("Bearer ") else ""
        if not secrets.compare_digest(supplied.encode(), self._token.encode()):
            raise HTTPException(401, "Bridge authorization required")

    def _job(self, execution_id):
        row = self.conn.execute("SELECT execution_id,message_id,phone,body,state,created_at,consumer_id,provider_reference,detail FROM whatsapp_delivery_jobs WHERE execution_id=?", (execution_id,)).fetchone()
        return dict(zip(("execution_id", "message_id", "phone", "body", "state", "created_at", "consumer_id", "provider_reference", "detail"), row)) if row else None

    def _public_job(self, job):
        return {key: job[key] for key in ("message_id", "execution_id", "state", "provider_reference", "detail")}

    def _notify(self, execution_id, event="update"):
        if self.on_update:
            try:
                self.on_update(self._public_job(self._job(execution_id)) | {"event": event})
            except Exception:
                _logger.warning("Delivery update listener failed; queue state was preserved.")

    def _set(self, execution_id, state, detail):
        self.conn.execute("UPDATE whatsapp_delivery_jobs SET state=?,detail=? WHERE execution_id=?", (state, detail, execution_id))
        self._notify(execution_id)

    def _heartbeat(self):
        row = self.conn.execute("SELECT consumer_id,connected,at FROM whatsapp_delivery_heartbeat WHERE id=1").fetchone()
        if not row:
            return None, False, None
        age = self.clock() - row[2]
        return row[0], bool(row[1]) and 0 <= age < 15, max(0, age)

    def status(self):
        _, connected, age = self._heartbeat()
        counts = dict(self.conn.execute("SELECT state,COUNT(*) FROM whatsapp_delivery_jobs GROUP BY state").fetchall())
        return {"connected": connected, "heartbeat_age_seconds": age, "queued": counts.get("queued", 0), "uncertain": counts.get("uncertain", 0)}

    async def enqueue(self, message_id, phone, body, execution_id):
        for value, limit in ((message_id, 128), (execution_id, 128), (body, 4096)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise HTTPException(400, "Invalid delivery payload")
        if not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            raise HTTPException(400, "A verified E.164 recipient is required")
        old = self._job(execution_id)
        if old:
            if (old["message_id"], old["phone"], old["body"]) != (message_id, phone, body):
                raise HTTPException(409, "Execution identifier is bound to a different payload")
            return {"status": old["state"], "provider_reference": execution_id}
        self.conn.execute("INSERT INTO whatsapp_delivery_jobs VALUES(?,?,?,?,'queued',?,NULL,NULL,?)",
                          (execution_id, message_id, phone, body, self.clock(), "Waiting for the connected WhatsApp bridge."))
        self._notify(execution_id)
        return {"status": "queued", "provider_reference": execution_id}

    def tick(self):
        for (execution_id,) in self.conn.execute("SELECT execution_id FROM whatsapp_delivery_jobs WHERE state='queued' AND created_at<=?", (self.clock() - 120,)).fetchall():
            self._set(execution_id, "expired", "Queued message expired before a bridge claimed it. No send was attempted.")
        for (execution_id,) in self.conn.execute("SELECT j.execution_id FROM whatsapp_delivery_jobs j JOIN whatsapp_delivery_attempts a ON a.execution_id=j.execution_id WHERE j.state='uncertain' AND j.detail=? AND a.claimed_at<=?",
                                                 (CLAIM_DETAIL, self.clock() - 30)).fetchall():
            self._set(execution_id, "uncertain", "The bridge did not confirm delivery before timeout. No automatic retry will be made.")

    def heartbeat(self, consumer_id, connected):
        current, fresh, _ = self._heartbeat()
        if fresh and current != consumer_id:
            raise HTTPException(409, "Another bridge owns the current connection")
        self.conn.execute("INSERT INTO whatsapp_delivery_heartbeat VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET consumer_id=excluded.consumer_id,connected=excluded.connected,at=excluded.at",
                          (consumer_id, int(connected), self.clock()))
        return {"connected": bool(connected)}

    def claim(self, consumer_id):
        self.tick()
        current, connected, _ = self._heartbeat()
        if not connected or current != consumer_id:
            raise HTTPException(409, "A fresh connected heartbeat from this bridge is required")
        rows = self.conn.execute("SELECT execution_id FROM whatsapp_delivery_jobs WHERE state='queued' ORDER BY created_at,rowid").fetchall()
        for (execution_id,) in rows:
            job = self._job(execution_id)
            if self.validate_job is not None:
                try:
                    valid = self.validate_job(job) is True
                except Exception:
                    valid = False
                if not valid:
                    self._set(execution_id, "skipped", "Message context changed before claim; no send was attempted.")
                    continue
            claimed = self.conn.execute("UPDATE whatsapp_delivery_jobs SET state='uncertain',consumer_id=?,detail=? WHERE execution_id=? AND state='queued'",
                                        (consumer_id, CLAIM_DETAIL, execution_id)).rowcount
            if claimed:
                self.conn.execute("INSERT OR REPLACE INTO whatsapp_delivery_attempts VALUES(?,?)", (execution_id, self.clock()))
                self._notify(execution_id, event="claim")
                return {"jobs": [{key: job[key] for key in ("execution_id", "message_id", "phone", "body")}]}
        return {"jobs": []}

    def receipt(self, execution_id, receipt):
        job = self._job(execution_id)
        if not job:
            raise HTTPException(404, "Delivery job not found")
        if job["consumer_id"] != receipt.consumer_id:
            raise HTTPException(409, "Receipt does not match the bridge that claimed this job")
        reference = receipt.provider_reference
        if reference is not None and not reference.strip():
            raise HTTPException(409, "Provider reference must be nonblank")
        if job["provider_reference"] and reference and job["provider_reference"] != reference:
            raise HTTPException(409, "Provider reference conflicts with the recorded receipt")
        if receipt.status in {"sent", "delivered", "read"} and not (reference or job["provider_reference"]):
            raise HTTPException(409, "A successful receipt requires its WhatsApp message reference")
        allowed = {"uncertain": {"uncertain", "sent", "delivered", "read", "failed"},
                   "sent": {"sent", "delivered", "read"}, "delivered": {"delivered", "read"},
                   "read": {"read"}, "failed": {"failed"}}
        if receipt.status not in allowed.get(job["state"], set()):
            raise HTTPException(409, "Receipt would regress the recorded delivery state")
        if (job["state"] != receipt.status or (reference and not job["provider_reference"]) or
                (receipt.detail is not None and receipt.detail != job["detail"])):
            detail = receipt.detail or {"uncertain": "Bridge could not confirm delivery. No retry will be made.",
                    "sent": "WhatsApp bridge confirmed sending; delivery is not confirmed.",
                    "delivered": "WhatsApp bridge confirmed delivery.", "read": "WhatsApp bridge confirmed the message was read.",
                    "failed": "Bridge reported failure. No automatic retry will be made."}[receipt.status]
            self.conn.execute("UPDATE whatsapp_delivery_jobs SET state=?,provider_reference=?,detail=? WHERE execution_id=?",
                              (receipt.status, reference or job["provider_reference"], detail, execution_id))
            self._notify(execution_id)
        return self._public_job(self._job(execution_id))

    async def watch(self):
        while True:
            async with self.lock:
                self.tick()
            await asyncio.sleep(1)


def router(service):
    routes = APIRouter(prefix="/api/whatsapp")

    @routes.get("/status")
    async def status():
        async with service.lock:
            service.tick()
            return service.status()

    @routes.post("/heartbeat")
    async def heartbeat(body: Heartbeat, authorization: str | None = Header(default=None)):
        service.authorize(authorization)
        async with service.lock:
            return service.heartbeat(body.consumer_id, body.connected)

    @routes.post("/jobs/claim")
    async def claim(body: Consumer, authorization: str | None = Header(default=None)):
        service.authorize(authorization)
        async with service.lock:
            return service.claim(body.consumer_id)

    @routes.post("/jobs/{execution_id}/receipt")
    async def receipt(execution_id: str, body: Receipt, authorization: str | None = Header(default=None)):
        service.authorize(authorization)
        async with service.lock:
            return service.receipt(execution_id, body)

    return routes
