"""Dashboard request API: stations, requests and their approve / reject / requote / resolve actions.

Projects agent tasks onto the `OfficeRequest` shape in packages/contracts, which the dashboard renders."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from office import agent, bus, config, db, environment_queries
from office.room_service import request_overlay
from office.room_messaging import RoomMessageConflict
from office.procurement_workflow import enrich_task
from office import procurement_workflow
from office_procurement.serialization import proposal_fingerprint


def request_view(conn, task_id: str) -> dict:
    raw = db.row(conn, "SELECT * FROM tasks WHERE id=?", task_id)
    if not raw:
        raise HTTPException(404, "Request not found")
    task = enrich_task(conn, db.task_view(raw))
    order = db.row(conn, "SELECT * FROM orders WHERE task_id=? ORDER BY rowid DESC LIMIT 1", task_id)
    ticket = db.row(conn, "SELECT * FROM tickets WHERE task_id=? ORDER BY rowid DESC LIMIT 1", task_id)
    status = task["status"]
    if status == "running":
        status = "processing"
    elif status == "awaiting_input" and raw["messages"] != "[]":
        status = "needs_clarification"
    elif status == "done":
        status = ("resolved" if order["status"] == "delivered" else "ordered") if order else (
            ("resolved" if ticket["status"] == "resolved" else "maintenance_open") if ticket else
            # A finished proposal with no order was refused, so nothing is left to do.
            "resolved" if task.get("proposal") or (task.get("grocery") or {}).get("proposal") or raw["messages"] != "[]" else "needs_clarification")
    proposal = task.get("proposal")
    return {
        "id": task_id, "station_id": task["board_id"], "user_id": None,
        "original_text": task["prompt"], "intent": "grocery" if task.get("grocery") else "purchase" if proposal else "maintenance" if ticket else None,
        "status": status, "summary": task.get("reply") or task["prompt"] or "Waiting for your request",
        "question": task.get("reply") if status == "needs_clarification" else None,
        "item_id": proposal["product"]["id"] if proposal else None,
        "proposal": proposal, "supplier_reference": order["ref"] if order else None,
        "simulation": not bool(task.get("grocery")), "assigned_to": ticket["team"] if ticket else None,
        "created_at": task["created_at"], "updated_at": task["updated_at"],
        "assessment": task.get("assessment"), "approval_required": task.get("approval_required", True),
        "approval": task.get("approval"),  # {type, approver, reason} from the company brain's routing table
        "research": task.get("research", []),
        "grocery": task.get("grocery"),
        **environment_queries.overlay(conn, task_id),
        **request_overlay(conn, task_id),
    }


def response(conn, task_id: str, duplicate=False):
    request = request_view(conn, task_id)
    command = db.row(conn, "SELECT * FROM board_commands WHERE board_id=?", request["station_id"])
    return {"request": request, "duplicate": duplicate,
            "command": {"station_id": request["station_id"],
                        "text": command["text"] if command else request["summary"],
                        "color": command["color"] if command else "amber"}}


class Transcript(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    event_id: str | None = Field(default=None, min_length=1, max_length=128)


class Approval(BaseModel):
    proposal_fingerprint: str = Field(min_length=1)


def router(conn):
    # Routes are async on purpose: they share one SQLite connection, and a plain def
    # would run on a worker thread, where parallel reads return errors or wrong rows.
    routes = APIRouter()

    @routes.get("/api/stations")
    async def stations():
        return config._load("config/stations.json")

    @routes.get("/api/requests")
    async def requests():
        return [request_view(conn, t["id"]) for t in db.rows(conn, "SELECT id FROM tasks ORDER BY rowid DESC LIMIT 200")]

    @routes.post("/api/requests/{task_id}/transcript")
    async def transcript(task_id: str, body: Transcript, request: Request):
        async with request.app.state.mutation_lock:
            rooms = request.app.state.rooms
            if config.MODEL == "mock":
                current = request_view(conn, task_id)
                try:
                    room = await rooms.try_handle(current["station_id"], body.text, task_id=task_id,
                        conversation_id=task_id if rooms._load(task_id) else None, event_id=body.event_id)
                except RoomMessageConflict as error:
                    raise HTTPException(409, str(error)) from error
                if room is not None:
                    return response(conn, room["id"])
            return await apply_transcript(task_id, body, rooms)

    async def apply_transcript(task_id: str, body: Transcript, rooms):
        current = request_view(conn, task_id)
        if config.MODEL != "mock":
            from office import conversation
            try:
                previous = conversation.input_receipt(conn, body.event_id, current["station_id"], body.text.strip(), task_id)
            except RoomMessageConflict as error:
                raise HTTPException(409, str(error)) from error
            if previous:
                return response(conn, previous, True)
        if (current["proposal"] or (current.get("grocery") or {}).get("proposal") or current["supplier_reference"]
                or current["assigned_to"] or procurement_workflow.execution_attempted(conn, task_id)
                or current["status"] not in {"awaiting_input", "needs_clarification", "failed", "resolved"}):
            raise HTTPException(409, "Request already has an action")
        if not body.text.strip():
            raise HTTPException(400, "Transcript must not be blank")
        if config.MODEL != "mock":
            conversation.input_receipt(conn, body.event_id, current["station_id"], body.text.strip(), task_id, task_id=task_id)
        conn.execute("UPDATE tasks SET prompt=? WHERE id=?", (body.text.strip(), task_id))
        result = await agent.run(conn, task_id, body.text.strip(), rooms=rooms, event_id=body.event_id,
                                 conversation_id=task_id if rooms._load(task_id) else None)
        if config.MODEL != "mock" and body.event_id:
            conn.execute("UPDATE conversation_inputs SET task_id=? WHERE id=?", (result["id"], body.event_id))
        return response(conn, result["id"])

    @routes.post("/api/requests/{task_id}/approve")
    async def approve(task_id: str, body: Approval, request: Request):
        async with request.app.state.mutation_lock:
            return await apply_approval(task_id, body)

    async def apply_approval(task_id: str, body: Approval):
        current = request_view(conn, task_id)
        proposal = current["proposal"] or (current.get("grocery") or {}).get("proposal")
        if not proposal or body.proposal_fingerprint != proposal_fingerprint(proposal):
            raise HTTPException(409, "Proposal changed; review it again")
        if current["status"] == "ordered":
            return current
        try:
            await agent.decide(conn, task_id, True, body.proposal_fingerprint)
        except agent.StaleApproval as error:
            raise HTTPException(409, str(error)) from error
        return request_view(conn, task_id)

    @routes.post("/api/requests/{task_id}/reject")
    async def reject(task_id: str, request: Request):
        """Decline a proposal directly, with no model call, so it works even if the quote expired."""
        async with request.app.state.mutation_lock:
            current = request_view(conn, task_id)
            if current["status"] != "awaiting_approval":
                raise HTTPException(409, "Only a request awaiting approval can be rejected")
            if db.row(conn, "SELECT 1 FROM procurement_attempts WHERE task_id=?", task_id):
                raise HTTPException(409, "Execution was attempted; reconcile it first")
            if current.get("grocery"):
                if db.row(conn, "SELECT 1 FROM oda_cart_attempts WHERE task_id=?", task_id):
                    raise HTTPException(409, "Cart update was attempted; reconcile it first")
                await agent.decide(conn, task_id, False, proposal_fingerprint(current["grocery"]["proposal"]))
                return request_view(conn, task_id)
            reply = "Not ordered: the proposal was rejected."
            db.set_command(conn, current["station_id"], reply, "amber")
            agent._update(conn, task_id, status="done", reply=reply, pending="[]")
            return request_view(conn, task_id)

    @routes.post("/api/requests/{task_id}/resolve")
    async def resolve(task_id: str):
        current = request_view(conn, task_id)
        if current["status"] not in {"ordered", "maintenance_open"}:
            raise HTTPException(409, "Only maintenance or ordered requests can be resolved")
        conn.execute("UPDATE orders SET status='delivered' WHERE task_id=?", (task_id,))
        conn.execute("UPDATE tickets SET status='resolved' WHERE task_id=?", (task_id,))
        bus.publish("state.changed", {})
        bus.publish("requests.updated", {"request_id": task_id})
        return request_view(conn, task_id)

    @routes.post("/api/requests/{task_id}/requote")
    async def requote(task_id: str, body: Approval, request: Request):
        async with request.app.state.mutation_lock:
            try:
                procurement_workflow.requote_task(conn, task_id, body.proposal_fingerprint)
            except procurement_workflow.ProcurementConflict as error:
                raise HTTPException(409, str(error)) from error
            return request_view(conn, task_id)

    return routes
