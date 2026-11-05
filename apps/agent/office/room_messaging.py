"""Context-bound room-owner drafts. Composition cannot choose a message recipient."""
import hashlib
import json
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class RoomMessageConflict(Exception):
    pass


class Booking(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    room_name: str = Field(min_length=1, max_length=200)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    owner_id: str = Field(min_length=1, max_length=128)
    revision: str | int
    simulation: bool

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("Booking must have a positive duration")
        return self


class Contact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    whatsapp_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{6,14}$")


class DraftDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["message_owner", "clarify", "cancel"]
    body: str | None = Field(default=None, max_length=2000)
    reply: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def meaningful_message(self):
        if not self.reply.strip() or (self.action == "message_owner" and not (self.body or "").strip()):
            raise ValueError("Decision requires a reply and messages require a body")
        return self


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _text(value, label, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise RoomMessageConflict(f"Invalid {label}")
    return value.strip()


class RoomMessageWorkflow:
    def __init__(self, conn, lookup_booking, lookup_contact, compose, sender=None):
        self.conn, self.lookup_booking, self.lookup_contact = conn, lookup_booking, lookup_contact
        self.compose, self.sender = compose, sender
        conn.execute("CREATE TABLE IF NOT EXISTS room_message_conversations(id TEXT PRIMARY KEY,body TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS room_message_events(id TEXT PRIMARY KEY,input TEXT NOT NULL,result TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS room_message_drafts(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,event_id TEXT NOT NULL UNIQUE,body TEXT NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,provider_reference TEXT)")

    def _event(self, event_id, value):
        event_id = _text(event_id, "event identifier", 128)
        encoded = _json(value)
        row = self.conn.execute("SELECT input,result FROM room_message_events WHERE id=?", (event_id,)).fetchone()
        if row:
            if row[0] != encoded:
                raise RoomMessageConflict("Event identifier is already bound to different input")
            if row[1] is None:
                raise RoomMessageConflict("Event composition is already in progress or interrupted; not retried")
            return json.loads(row[1])
        self.conn.execute("INSERT INTO room_message_events VALUES(?,?,NULL)", (event_id, encoded))
        return None

    def _result(self, event_id, result):
        self.conn.execute("UPDATE room_message_events SET result=? WHERE id=?", (_json(result), event_id))
        return result

    def _lookup(self, booking_id):
        try:
            raw = self.lookup_booking(booking_id)
            if raw is None or isinstance(raw, (list, tuple, set)):
                raise RoomMessageConflict("Booking is missing or ambiguous")
            booking = Booking.model_validate(raw)
            if booking.id != booking_id:
                raise RoomMessageConflict("Booking lookup returned a different booking")
            raw_contact = self.lookup_contact(booking.owner_id)
            if raw_contact is None or isinstance(raw_contact, (list, tuple, set)):
                raise RoomMessageConflict("Booking owner is missing or ambiguous")
            contact = Contact.model_validate(raw_contact)
            if contact.id != booking.owner_id:
                raise RoomMessageConflict("Contact does not match the verified booking owner")
            return booking.model_dump(mode="json"), contact.model_dump(mode="json")
        except RoomMessageConflict:
            raise
        except Exception:
            raise RoomMessageConflict("Booking or contact could not be verified") from None

    def _conversation(self, conversation_id):
        row = self.conn.execute("SELECT body FROM room_message_conversations WHERE id=?", (conversation_id,)).fetchone()
        if not row:
            raise RoomMessageConflict("Conversation does not exist")
        value = json.loads(row[0])
        booking, contact = self._lookup(value["booking"]["id"])
        if _json(booking) != _json(value["booking"]) or _json(contact) != _json(value["contact"]):
            raise RoomMessageConflict("Booking or owner contact changed; start a new conversation")
        return value

    def open_conflict(self, event_id, booking_id, requester_name):
        event_id = _text(event_id, "event identifier", 128)
        booking_id = _text(booking_id, "booking identifier", 128)
        requester_name = _text(requester_name, "requester name", 200)
        booking, contact = self._lookup(booking_id)
        previous = self._event(event_id, {"kind": "open", "booking_id": booking_id, "requester_name": requester_name})
        if previous:
            return previous["conversation_id"]
        conversation_id = uuid4().hex
        value = {"booking": booking, "contact": contact, "requester_name": requester_name}
        self.conn.execute("INSERT INTO room_message_conversations VALUES(?,?)", (conversation_id, _json(value)))
        self._result(event_id, {"conversation_id": conversation_id})
        return conversation_id

    async def follow_up(self, conversation_id, event_id, text):
        event_id = _text(event_id, "event identifier", 128)
        text = _text(text, "instruction")
        previous = self._event(event_id, {"kind": "follow_up", "conversation_id": conversation_id, "text": text})
        if previous:
            return previous
        result = {"id": uuid4().hex, "conversation_id": conversation_id, "status": "failed",
                  "reply": "The message could not be composed. No message was sent."}
        try:
            value = self._conversation(conversation_id)
            context = {"instruction": text, "requester_name": value["requester_name"], "booking": value["booking"],
                       "owner": {key: value["contact"][key] for key in ("id", "name")}}
            decision = DraftDecision.model_validate(await self.compose(context))
            self._conversation(conversation_id)  # Recheck after the model await.
        except RoomMessageConflict:
            self._result(event_id, result)
            raise
        except Exception:
            return self._result(event_id, result)
        result |= {"status": {"message_owner": "drafted", "clarify": "clarification", "cancel": "cancelled"}[decision.action],
                   "reply": decision.reply}
        if decision.action == "message_owner":
            prefix = "Office assistant test (simulated booking)\n" if value["booking"]["simulation"] else ""
            result |= {"body": prefix + decision.body.strip(), "recipient_id": value["contact"]["id"]}
            fingerprint = hashlib.sha256(_json({"draft": result, "context": value}).encode()).hexdigest()
            result["fingerprint"] = fingerprint
            self.conn.execute("INSERT INTO room_message_drafts VALUES(?,?,?,?,?,'drafted',NULL)",
                              (result["id"], conversation_id, event_id, _json(result), fingerprint))
        return self._result(event_id, result)

    async def dispatch(self, message_id, fingerprint):
        row = self.conn.execute("SELECT conversation_id,body,fingerprint,status,provider_reference FROM room_message_drafts WHERE id=?", (message_id,)).fetchone()
        if not row:
            raise RoomMessageConflict("Message draft does not exist")
        conversation_id, encoded, exact, status, reference = row
        if not isinstance(fingerprint, str) or fingerprint != exact:
            raise RoomMessageConflict("Message draft changed; review its exact fingerprint")
        result = {"id": message_id, "conversation_id": conversation_id, "status": status}
        if reference:
            result["provider_reference"] = reference
        if status in {"queued", "sent", "delivered", "read", "uncertain", "failed", "expired", "skipped"}:
            return result
        value = self._conversation(conversation_id)
        phone = value["contact"]["whatsapp_number"]
        if self.sender is None or not phone:
            raise RoomMessageConflict("WhatsApp sender or verified owner number is not configured")
        claimed = self.conn.execute("UPDATE room_message_drafts SET status='uncertain' WHERE id=? AND status='drafted'", (message_id,)).rowcount
        result["status"] = "uncertain"
        if not claimed:
            return result
        try:
            response = await self.sender(phone, json.loads(encoded)["body"], message_id)
            if not isinstance(response, dict) or response.get("status") not in {"queued", "accepted", "delivered"}:
                return result
            reference = response.get("provider_reference")
            if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
                return result
            status = response["status"] if response["status"] in {"queued", "delivered"} else "sent"
            self.conn.execute("UPDATE room_message_drafts SET status=?,provider_reference=? WHERE id=?", (status, reference, message_id))
            return result | {"status": status, "provider_reference": reference}
        except Exception:
            return result
