"""Configurable WhatsApp alerts for out-of-order equipment.

Rules are edited in the dashboard: "when <equipment> is out of order, send WhatsApp to
<person, phone>". Each new maintenance ticket (tickets table) is matched against the
enabled rules by keywords in the ticket summary and the reported text. A match queues
exactly one message per (ticket, rule) through the existing local WhatsApp bridge
(whatsapp_delivery). No message is resent; the bridge's own receipts report outcome.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from office import db

DEFAULT_RULES = [
    {"label": "Toalett", "keywords": ["toalett", "toilet", "wc", "do'en", "doen", "dass"]},
    {"label": "Oppvaskmaskin", "keywords": ["oppvaskmaskin", "oppvaskmaskinen", "dishwasher"]},
]


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)
    keywords: list[str] = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=80)
    phone: str = Field(min_length=8, max_length=16)
    enabled: bool = True

    @field_validator("phone")
    @classmethod
    def e164(cls, value: str) -> str:
        value = re.sub(r"[\s-]", "", value)
        if re.fullmatch(r"[1-9][0-9]{7}", value):  # Norwegian 8-digit number without country code
            value = "+47" + value
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", value):
            raise ValueError("Phone must be in international format, e.g. +47 12345678")
        return value

    @field_validator("keywords")
    @classmethod
    def clean(cls, value: list[str]) -> list[str]:
        words = [w.strip().casefold()[:40] for w in value if w.strip()]
        if not words:
            raise ValueError("At least one keyword is required")
        return words


def _mask(phone: str) -> str:
    return phone[:3] + " ••• " + phone[-2:] if len(phone) > 6 else "•••"


class AlertRules:
    def __init__(self, conn, lock, delivery=None, clock=time.time):
        self.conn, self.lock, self.delivery, self.clock = conn, lock, delivery, clock
        conn.execute("CREATE TABLE IF NOT EXISTS alert_rules(id TEXT PRIMARY KEY,label TEXT NOT NULL,keywords TEXT NOT NULL,"
                     "name TEXT NOT NULL,phone TEXT NOT NULL,enabled INTEGER NOT NULL,created_at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS alert_sent(execution_id TEXT PRIMARY KEY,ticket_id TEXT NOT NULL,rule_id TEXT NOT NULL,"
                     "label TEXT NOT NULL,name TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL,at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS alert_cursor(id INTEGER PRIMARY KEY CHECK(id=1),ticket_rowid INTEGER NOT NULL)")
        # Only tickets created after the feature starts are alerted; history is never replayed.
        if not conn.execute("SELECT 1 FROM alert_cursor").fetchone():
            last = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM tickets").fetchone()[0]
            conn.execute("INSERT INTO alert_cursor VALUES(1,?)", (last,))

    # ---- rules -------------------------------------------------------------
    def rules(self, reveal=False):
        out = []
        for row in self.conn.execute("SELECT id,label,keywords,name,phone,enabled,created_at FROM alert_rules ORDER BY created_at"):
            rule = dict(zip(("id", "label", "keywords", "name", "phone", "enabled", "created_at"), row))
            rule["keywords"], rule["enabled"] = json.loads(rule["keywords"]), bool(rule["enabled"])
            if not reveal:
                rule["phone"] = _mask(rule["phone"])
            out.append(rule)
        return out

    def save(self, rule_id, rule: RuleIn):
        rule_id = rule_id or uuid.uuid4().hex[:12]
        self.conn.execute("INSERT INTO alert_rules VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,"
                          "keywords=excluded.keywords,name=excluded.name,phone=excluded.phone,enabled=excluded.enabled",
                          (rule_id, rule.label, json.dumps(rule.keywords), rule.name, rule.phone, int(rule.enabled), self.clock()))
        return next(r for r in self.rules() if r["id"] == rule_id)

    def delete(self, rule_id):
        if not self.conn.execute("DELETE FROM alert_rules WHERE id=?", (rule_id,)).rowcount:
            raise HTTPException(404, "Rule not found")

    def sent(self, limit=20):
        states = {}
        if self.delivery is not None:
            for execution_id, state in self.conn.execute("SELECT execution_id,state FROM whatsapp_delivery_jobs WHERE execution_id LIKE 'alert-%'"):
                states[execution_id] = state
        rows = self.conn.execute("SELECT execution_id,ticket_id,label,name,body,state,at FROM alert_sent ORDER BY at DESC LIMIT ?", (limit,))
        return [{"execution_id": e, "ticket_id": t, "label": l, "name": n, "body": b, "state": states.get(e, s), "at": a}
                for e, t, l, n, b, s, a in rows]

    # ---- matching ----------------------------------------------------------
    @staticmethod
    def matches(rule, text: str) -> bool:
        t = text.casefold()
        return any(re.search(r"(?<!\w)" + re.escape(k), t) for k in rule["keywords"])  # prefix: "toalett" matches "toalettet"

    def _validate(self, job) -> bool:
        """Fail-closed claim check for alert jobs: the row must exist with the same recipient and text."""
        row = self.conn.execute("SELECT a.body,r.phone,r.enabled FROM alert_sent a JOIN alert_rules r ON r.id=a.rule_id WHERE a.execution_id=?",
                                (job.get("execution_id"),)).fetchone()
        return bool(row and row[0] == job.get("body") and row[1] == job.get("phone") and row[2])

    async def check(self):
        """Match tickets created since the cursor; queue one WhatsApp per (ticket, rule)."""
        cursor = self.conn.execute("SELECT ticket_rowid FROM alert_cursor").fetchone()[0]
        rows = self.conn.execute("SELECT t.rowid,t.id,t.board_id,t.summary,t.team,COALESCE(k.prompt,'') FROM tickets t "
                                 "LEFT JOIN tasks k ON k.id=t.task_id WHERE t.rowid>? ORDER BY t.rowid", (cursor,)).fetchall()
        rules = [r for r in self.rules(reveal=True) if r["enabled"]]
        for rowid, ticket_id, board_id, summary, team, prompt in rows:
            for rule in rules:
                if not self.matches(rule, f"{summary} {prompt}"):
                    continue
                execution_id = f"alert-{ticket_id}-{rule['id']}"[:128]
                if self.conn.execute("SELECT 1 FROM alert_sent WHERE execution_id=?", (execution_id,)).fetchone():
                    continue
                reported = f'\nMeldt: "{prompt.strip()[:200]}"' if prompt.strip() and not prompt.startswith("[sensor]") else ""
                body = (f"⚠️ {rule['label']} ser ut til å være ute av drift ({board_id}).\n"
                        f"{summary.strip()[:300]}{reported}\nSak er opprettet for {team}. – Office-assistenten")
                state = "no_bridge"
                if self.delivery is not None:
                    try:
                        state = (await self.delivery.enqueue(execution_id, rule["phone"], body, execution_id))["status"]
                    except HTTPException as error:
                        state = f"rejected: {error.detail}"
                self.conn.execute("INSERT INTO alert_sent VALUES(?,?,?,?,?,?,?,?)",
                                  (execution_id, ticket_id, rule["id"], rule["label"], rule["name"], body, state, self.clock()))
            self.conn.execute("UPDATE alert_cursor SET ticket_rowid=? WHERE id=1", (rowid,))

    async def watch(self, interval=2.0):
        while True:
            try:
                async with self.lock:
                    await self.check()
            except Exception:  # never stop watching because of one bad row
                pass
            await asyncio.sleep(interval)


def router(service: AlertRules) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/alerts/rules")
    async def list_rules():
        async with service.lock:
            return {"rules": service.rules(), "sent": service.sent(),
                    "whatsapp": service.delivery.status() if service.delivery else {"connected": False, "configured": False},
                    "suggestions": DEFAULT_RULES}

    @routes.post("/api/alerts/rules")
    async def create_rule(body: RuleIn):
        async with service.lock:
            return service.save(None, body)

    @routes.put("/api/alerts/rules/{rule_id}")
    async def update_rule(rule_id: str, body: RuleIn):
        async with service.lock:
            if not any(r["id"] == rule_id for r in service.rules()):
                raise HTTPException(404, "Rule not found")
            return service.save(rule_id, body)

    @routes.post("/api/alerts/rules/{rule_id}/enabled")
    async def toggle_rule(rule_id: str, body: dict):
        async with service.lock:
            if not service.conn.execute("UPDATE alert_rules SET enabled=? WHERE id=?", (int(bool(body.get("enabled"))), rule_id)).rowcount:
                raise HTTPException(404, "Rule not found")
            return next(r for r in service.rules() if r["id"] == rule_id)

    @routes.delete("/api/alerts/rules/{rule_id}")
    async def delete_rule(rule_id: str):
        async with service.lock:
            service.delete(rule_id)
            return {"deleted": rule_id}

    return routes
