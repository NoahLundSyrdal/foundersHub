"""One exact-snapshot WhatsApp approval link; GET never approves or pays an order."""
import asyncio
import hashlib
import html
import json
import logging
import re
import secrets
import time
import traceback
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from office import agent, db, procurement_workflow as workflow
from office_procurement.serialization import epoch, proposal_fingerprint

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PATH_TOKEN = re.compile(r"(/confirm/)[A-Za-z0-9_-]{20,}")
_logger = logging.getLogger(__name__)


class ConfirmationTokenFilter(logging.Filter):
    """Attach to HTTP access/error handlers so capability URLs never enter logs."""
    def filter(self, record):
        record.msg = _PATH_TOKEN.sub(r"\1[redacted]", str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(_PATH_TOKEN.sub(r"\1[redacted]", value) if isinstance(value, str) else value for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _PATH_TOKEN.sub(r"\1[redacted]", value) if isinstance(value, str) else value for key, value in record.args.items()}
        if record.exc_info:
            record.exc_text = _PATH_TOKEN.sub(r"\1[redacted]", "".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = _PATH_TOKEN.sub(r"\1[redacted]", record.exc_text)
        return True


def _amount(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError
        return number
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(409, "Tilbudet har en ugyldig pris.") from None


def _money(value):
    return f"{_amount(value):.2f}".replace(".", ",") + " kr"


def _basket(value):
    grocery = value.get("grocery") or {}
    proposal = grocery.get("proposal") or value.get("proposal")
    if not isinstance(proposal, dict) or proposal.get("currency", "NOK") != "NOK":
        raise HTTPException(409, "Ingen bestilling venter på bekreftelse.")
    real = bool(grocery.get("proposal"))
    if real:
        lines = proposal.get("lines")
        if not isinstance(lines, list) or not 1 <= len(lines) <= 10:
            raise HTTPException(409, "Tilbudet har ugyldige varelinjer.")
        total = _amount(proposal.get("subtotal_nok"))
        display = []
        summed = Decimal(0)
        for line in lines:
            quantity = line.get("quantity")
            name = line.get("name")
            if type(quantity) is not int or not 1 <= quantity <= 20 or not isinstance(name, str) or not 1 <= len(name) <= 200:
                raise HTTPException(409, "Tilbudet har ugyldige varelinjer.")
            unit = _amount(line.get("unit_price_nok"))
            summed += unit * quantity
            display.append(f"{quantity} × {name} à {_money(unit)} = {_money(unit * quantity)}")
        if summed != total:
            raise HTTPException(409, "Tilbudets priser må kontrolleres på nytt.")
    else:
        product, quantity = proposal.get("product") or {}, proposal.get("quantity")
        if proposal.get("simulation") is not True or type(quantity) is not int or not 1 <= quantity <= 20 or not isinstance(product.get("name"), str):
            raise HTTPException(409, "Dette tilbudet kan ikke bekreftes her.")
        total = _amount(proposal.get("total_nok"))
        display = [f"{quantity} × {product['name']} à {_money(product.get('price_nok'))}"]
    return proposal, real, display, total


class OrderConfirmations:
    def __init__(self, conn, lock, delivery, *, recipient: str, public_base_url: str, clock=time.time):
        if not isinstance(recipient, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", recipient):
            raise ValueError("A static verified E.164 recipient is required")
        parsed = urlsplit(public_base_url)
        local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not local) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("A public HTTPS URL or local development URL is required")
        self.conn, self.lock, self.delivery = conn, lock, delivery
        self.recipient, self.public_base_url, self.clock = recipient, public_base_url.rstrip("/"), clock
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        workflow.ensure_schema(conn)
        conn.execute("""CREATE TABLE IF NOT EXISTS order_confirmations(
          message_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,fingerprint TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,
          recipient TEXT NOT NULL,expires_at REAL NOT NULL,created_at REAL NOT NULL,state TEXT NOT NULL,
          UNIQUE(task_id,fingerprint))""")
        self._startup_rowid = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM tasks").fetchone()[0]

    def _row(self, token):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise HTTPException(404, "Bekreftelsen finnes ikke.")
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = db.row(self.conn, "SELECT * FROM order_confirmations WHERE token_hash=?", digest)
        if not row:
            raise HTTPException(404, "Bekreftelsen finnes ikke.")
        return row

    def _current(self, row, *, pending=True):
        if row["expires_at"] <= self.clock():
            raise HTTPException(409, "Bekreftelsen har utløpt. Be om et nytt tilbud.")
        task = db.row(self.conn, "SELECT * FROM tasks WHERE id=?", row["task_id"])
        value = workflow.get_context(self.conn, row["task_id"])
        if not task or not value:
            raise HTTPException(409, "Bestillingen er ikke lenger tilgjengelig.")
        if row["state"] == "invalid":
            raise HTTPException(409, "Tilbudet kan ikke godkjennes. Kontroller det på nytt.")
        if not pending and row["state"] == "rejected" and not value.get("proposal") and not (value.get("grocery") or {}).get("proposal"):
            # Declining may release the executable quote. The fingerprint itself is
            # its exact JSON snapshot; restore it only in this read-only page value.
            value = value | {"proposal": json.loads(row["fingerprint"])}
        proposal, _, _, _ = _basket(value)
        if proposal_fingerprint(proposal) != row["fingerprint"]:
            raise HTTPException(409, "Tilbudet har endret seg. Se gjennom et nytt tilbud.")
        try:
            expired = epoch(proposal["expires_at"]) <= self.clock()
        except (KeyError, TypeError, ValueError):
            expired = True
        if expired:
            raise HTTPException(409, "Tilbudet har utløpt. Be om et nytt tilbud.")
        if pending and (row["state"] != "pending" or task["status"] != "awaiting_approval" or workflow.execution_attempted(self.conn, task["id"])):
            raise HTTPException(409, "Bestillingen kan ikke bekreftes på nytt. Kontroller utfallet før du prøver igjen.")
        return task, value

    @staticmethod
    def _public(row):
        return {key: row[key] for key in ("message_id", "task_id", "state", "expires_at")}

    async def queue_request(self, task_id):
        """Queue only; caller must not already hold the shared mutation lock."""
        async with self.lock:
            task = db.row(self.conn, "SELECT * FROM tasks WHERE id=?", task_id)
            value = workflow.get_context(self.conn, task_id)
            if not task or not value or task["status"] != "awaiting_approval" or workflow.execution_attempted(self.conn, task_id):
                raise HTTPException(409, "Ingen uutført bestilling venter på bekreftelse.")
            proposal, real, lines, total = _basket(value)
            fingerprint = proposal_fingerprint(proposal)
            old = db.row(self.conn, "SELECT * FROM order_confirmations WHERE task_id=? AND fingerprint=?", task_id, fingerprint)
            if old:
                return self._public(old)
            try:
                expires = min(epoch(proposal["expires_at"]), self.clock() + 900)
            except (KeyError, TypeError, ValueError):
                raise HTTPException(409, "Tilbudet mangler gyldig utløpstid.") from None
            if expires <= self.clock():
                raise HTTPException(409, "Tilbudet har utløpt.")
            token = secrets.token_urlsafe(32)
            message_id = "order-confirm-" + hashlib.sha256((task_id + "\0" + fingerprint).encode()).hexdigest()[:48]
            body = ("Bekreft Oda-handlekurv (ikke betalt bestilling)" if real else "Bekreft SIMULERT bestilling") + "\n"
            body += "\n".join(lines) + f"\n{'Varesum' if real else 'Total'}: {_money(total)}.\n"
            body += ("Levering og gebyrer avklares hos Oda. Bekreftelsen legger kun varer i handlekurven; du betaler hos Oda.\n" if real
                     else "Dette er en simulering. Ingen reell levering eller betaling blir utført.\n")
            body += "Åpne og bekreft det nøyaktige tilbudet: " + self.public_base_url + "/confirm/" + token
            if len(body) > 4096:
                raise HTTPException(409, "Tilbudet er for langt for én bekreftelsesmelding.")
            # WhatsAppDelivery.enqueue is a local SQLite queue, not a provider send.
            # Same-connection transaction makes the token hash and outbound job atomic.
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("INSERT INTO order_confirmations VALUES(?,?,?,?,?,?,?,'pending')",
                                  (message_id, task_id, fingerprint, hashlib.sha256(token.encode()).hexdigest(), self.recipient, expires, self.clock()))
                await self.delivery.enqueue(message_id, self.recipient, body, message_id)
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            return self._public(db.row(self.conn, "SELECT * FROM order_confirmations WHERE message_id=?", message_id))

    def validate_job(self, job):
        """Synchronous fail-closed hook for the local bridge's final claim check."""
        try:
            row = db.row(self.conn, "SELECT * FROM order_confirmations WHERE message_id=?", job.get("message_id"))
            if not row or job.get("execution_id") != row["message_id"] or job.get("phone") != self.recipient or row["recipient"] != self.recipient:
                return False
            self._current(row)
            return True
        except Exception:
            return False

    async def watch(self):
        while True:
            async with self.lock:
                tasks = [r[0] for r in self.conn.execute("SELECT id FROM tasks WHERE rowid>? AND status='awaiting_approval' ORDER BY rowid LIMIT 100", (self._startup_rowid,))]
            for task_id in tasks:
                try:
                    await self.queue_request(task_id)
                except HTTPException:
                    pass  # stale, expired or already attempted: never send a misleading approval
                except Exception:
                    _logger.warning("Order confirmation could not be queued; no approval was executed.")
            await asyncio.sleep(1)

    def _page(self, token, row, task, value, *, outcome=False):
        _, real, lines, total = _basket(value)
        title = "Oda-handlekurv" if real else "SIMULERT bestilling"
        items = "".join("<li>" + html.escape(line) + "</li>" for line in lines)
        notice = "Levering og gebyrer avklares hos Oda. Dette er ikke en betalt ordre." if real else "Dette er en simulering. Ingen reell bestilling eller betaling blir utført."
        action = html.escape(self.public_base_url + "/confirm/" + token, quote=True)
        controls = (f'<form method="post" action="{action}"><button type="submit">Bekreft dette tilbudet</button></form>'
                    f'<form method="post" action="{action}/reject"><button type="submit">Avslå</button></form>')
        if outcome:
            controls = "<p>" + html.escape(task.get("reply") or "Bekreftelsen er allerede behandlet.") + "</p>"
            grocery = value.get("grocery") or {}
            if task["status"] == "checkout_ready" and grocery.get("state") == "cart_ready":
                url = grocery.get("cart_url")
                parsed = urlsplit(url) if isinstance(url, str) else None
                if parsed and parsed.scheme == "https" and parsed.hostname == "oda.com" and not parsed.username and not parsed.password:
                    controls += f'<p><a href="{html.escape(url, quote=True)}" rel="noreferrer">Se levering og betal hos Oda</a></p>'
            if row["state"] in {"processing", "uncertain"}:
                controls += "<p>Utfallet må kontrolleres. Denne lenken prøver ikke bestillingen på nytt.</p>"
        content = f'<!doctype html><html lang="nb"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><body><main><h1>{title}</h1><ul>{items}</ul><p>{"Varesum" if real else "Total"}: {_money(total)}</p><p>{notice}</p>{controls}</main></body></html>'
        return HTMLResponse(content, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'", "X-Content-Type-Options": "nosniff"})

    async def show(self, token):
        async with self.lock:
            row = self._row(token)
            task, value = self._current(row, pending=row["state"] == "pending")
            return self._page(token, row, task, value, outcome=row["state"] != "pending")

    async def decide(self, token, approve):
        async with self.lock:
            row = self._row(token)
            if row["state"] != "pending":
                task, value = self._current(row, pending=False)
                return self._page(token, row, task, value, outcome=True)
            task, value = self._current(row)
            changed = self.conn.execute("UPDATE order_confirmations SET state='processing' WHERE message_id=? AND state='pending'", (row["message_id"],)).rowcount
            if not changed:
                raise HTTPException(409, "Bekreftelsen behandles allerede.")
            try:
                result = await agent.decide(self.conn, task["id"], approve, row["fingerprint"])
            except (agent.StaleApproval, workflow.ProcurementConflict):
                self.conn.execute("UPDATE order_confirmations SET state='invalid' WHERE message_id=?", (row["message_id"],))
                raise HTTPException(409, "Tilbudet kan ikke godkjennes. Kontroller det på nytt.") from None
            except BaseException:
                self.conn.execute("UPDATE order_confirmations SET state='uncertain' WHERE message_id=?", (row["message_id"],))
                raise HTTPException(409, "Utfallet er usikkert. Kontroller bestillingen; lenken prøver ikke på nytt.") from None
            grocery = result.get("grocery") or {}
            state = "rejected" if not approve else "uncertain" if result.get("status") == "failed" or grocery.get("state") == "uncertain" else "accepted"
            self.conn.execute("UPDATE order_confirmations SET state=? WHERE message_id=?", (state, row["message_id"]))
            current = self._row(token)
            task, value = self._current(current, pending=False)
            return self._page(token, current, task, value, outcome=True)


def router(service):
    routes = APIRouter()

    @routes.api_route("/confirm/{token}", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def show(token: str):
        return await service.show(token)

    def same_origin(request):
        origin = request.headers.get("origin")
        if origin and origin != service.origin:
            raise HTTPException(403, "Bekreftelsen må sendes fra denne siden.")

    @routes.post("/confirm/{token}", response_class=HTMLResponse)
    async def confirm(token: str, request: Request):
        same_origin(request)
        return await service.decide(token, True)

    @routes.post("/confirm/{token}/reject", response_class=HTMLResponse)
    async def reject(token: str, request: Request):
        same_origin(request)
        return await service.decide(token, False)

    return routes
