"""Persistent dashboard conversations using the existing Pydantic AI agent."""
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, TextPart
from office import agent, config, db, requests_api

class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)
    board_id: str = "office"


def router(conn, lock):
    conn.execute("CREATE TABLE IF NOT EXISTS chat_turns(event_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, task_id TEXT NOT NULL, body TEXT NOT NULL)")
    routes = APIRouter(prefix="/api/chat")

    @routes.get("/{conversation_id}")
    async def history(conversation_id: str):
        async with lock:
            return {"requests": [requests_api.request_view(conn, r["task_id"]) for r in db.rows(conn,
                "SELECT task_id FROM chat_turns WHERE conversation_id=? ORDER BY rowid", conversation_id)]}

    @routes.post("")
    async def send(body: ChatMessage):
        if not body.text.strip():
            raise HTTPException(422, "Message must not be blank")
        if body.board_id not in config.BOARDS:
            raise HTTPException(404, "Unknown location")
        payload = json.dumps(body.model_dump(), sort_keys=True)
        async with lock:
            prior = db.row(conn, "SELECT * FROM chat_turns WHERE event_id=?", body.event_id)
            if prior:
                if prior["body"] != payload:
                    raise HTTPException(409, "Message ID was already used for different content")
                return requests_api.response(conn, prior["task_id"], True)
            # Carry conversational turns, not unresolved purchase tool calls, between tasks.
            # Approval still resumes the original task's complete stored Pydantic messages.
            turns = db.rows(conn, "SELECT t.* FROM chat_turns c JOIN tasks t ON t.id=c.task_id WHERE c.conversation_id=? ORDER BY c.rowid DESC LIMIT 40", body.conversation_id)
            messages = []
            for turn in reversed(turns):
                messages.extend([ModelRequest(parts=[UserPromptPart(turn["prompt"])]),
                    ModelResponse(parts=[TextPart((turn["reply"] or "No response recorded") + f"\n[Request {turn['id']}; current status: {turn['status']}]")])])
            task_id = agent.start_task(conn, body.board_id, config.GUEST.name, body.text.strip())
            conn.execute("INSERT INTO chat_turns VALUES(?,?,?,?)", (body.event_id, body.conversation_id, task_id, payload))
            await agent.run(conn, task_id, body.text.strip(), chat_history=messages)
            return requests_api.response(conn, task_id)
    return routes
