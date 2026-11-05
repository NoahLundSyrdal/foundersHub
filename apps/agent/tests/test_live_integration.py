"""Live conversation engine (office/conversation.py) with the merged voice, approval-email and chat features."""
import base64

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from office import approval_email, conversation, voice
from office.speaker import Speaker
from test_live_conversation import assessment, live, reply, returned  # noqa: F401  (live is a fixture)


def instructions(messages, info) -> str:
    parts = [p.content for m in messages if isinstance(m, ModelRequest) for p in (m.instructions or []) if hasattr(p, "content")]
    return " ".join(parts) + (info.instructions or "")


def test_voice_speaker_reaches_claude_and_the_approval_email(live, monkeypatch):
    client, app, _ = live
    monkeypatch.setattr(voice, "transcribe", lambda audio: "We need two packs of cups")
    monkeypatch.setattr(voice, "identify", lambda audio: Speaker("leo", "voice_id", 0.82, 0.3, "match"))
    emailed = []
    monkeypatch.setattr(approval_email, "notify", lambda conn, task_id: emailed.append(task_id))
    seen = []
    decision = {"intent": "purchase", "item_id": "cups", "summary": "More cups", "quantity": 2,
                "question": None, "assessment": assessment()}

    def model(messages, info):
        seen.append(instructions(messages, info))
        if not returned(messages, "submit_request"):
            return ModelResponse(parts=[ToolCallPart("submit_request", {"decision": decision})])
        return reply(info, "Two packs of cups are proposed for approval (simulated).")

    with conversation.assistant.override(model=FunctionModel(model)):
        audio = base64.b64encode(b"clip").decode()
        task = client.post("/api/input", json={"board_id": "kitchen", "audio_b64": audio}).json()
    assert task["status"] == "awaiting_approval"
    assert task["speaker"]["source"] == "voice_id" and task["person"] == "Leo"
    assert task["pending"][0]["details"]["requester"]["source"] == "voice_id"
    assert any("Leo (office manager), identified by voice" in s for s in seen)
    assert emailed == [task["id"]]


def test_dashboard_chat_history_reaches_the_live_engine(live):
    client, _, _ = live
    prompts = []

    def model(messages, info):
        prompts.append([p.content for m in messages if isinstance(m, ModelRequest)
                        for p in m.parts if isinstance(p, UserPromptPart)])
        return reply(info, "Noted.")

    with conversation.assistant.override(model=FunctionModel(model)):
        for n, text in enumerate(["Is the kitchen warm?", "And yesterday?"]):
            r = client.post("/api/chat", json={"conversation_id": "c1", "event_id": f"e{n}", "text": text, "board_id": "kitchen"})
            assert r.status_code == 200, r.text
    assert prompts[-1][:2] == ["Is the kitchen warm?", "And yesterday?"]
