"""Who is speaking, and how we know. Owner: voice.

`Speaker` is the annotation every request carries: it is persisted per task, lives on `Deps` for tools and
server policy, appears in approval details, and reaches the model through the `SpeakerContext` capability.

A speaker label is context and *restrictive* access control only. Voice ID and dashboard picks are not
authentication: nothing may grant authority (spending, approval, bypassing policy) because of who spoke.
Tools that need a known requester opt in with tool metadata `{"speaker": "identified"}`; `SpeakerContext`
hides them from the model, and therefore blocks their execution, when the speaker is not identified.
"""

import json
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import InstructionPart

from office import config

Source = Literal["voice_id", "declared", "none"]  # voice service | dashboard/device pick | no identity
# Reasons the voice service reports, plus client-side failures. Anything else is normalised to "unavailable".
REASONS = {"match", "below_threshold", "ambiguous", "too_short", "no_enrollments", "not_enrolled",
           "not_configured", "unavailable"}


@dataclass(frozen=True)
class Speaker:
    name: str | None  # config.PEOPLE key (lower-cased), or None when unknown
    source: Source
    score: float | None = None  # voice ID cosine score of the best match
    margin: float | None = None  # lead over the runner-up
    reason: str | None = None  # voice ID decision reason

    @classmethod
    def declared(cls, name: str | None) -> "Speaker":
        """A name picked on the dashboard or sent by a device. Unverified, like every label."""
        key = (name or "").strip().lower()
        return cls(key, "declared") if key in config.PEOPLE else cls.unknown()

    @classmethod
    def unknown(cls, reason: str | None = None) -> "Speaker":
        return cls(None, "none", reason=reason)

    @classmethod
    def from_voice_id(cls, result: dict) -> "Speaker":
        """The voice service's /identify response → Speaker. Names outside config/people.json are unknown."""
        name = (result.get("speaker") or "").lower() or None
        reason = result.get("reason") if result.get("reason") in REASONS else "unavailable"
        if name is not None and name not in config.PEOPLE:
            name, reason = None, "not_enrolled"
        score, margin = result.get("score"), result.get("margin")
        return cls(name, "voice_id" if name else "none",
                   float(score) if isinstance(score, int | float) else None,
                   float(margin) if isinstance(margin, int | float) else None, reason)

    @property
    def identified(self) -> bool:
        return self.name is not None

    @property
    def person(self) -> config.Person:
        return config.person(self.name)

    def annotation(self) -> dict:
        """JSON for task views and approval details: who, and on what evidence."""
        return asdict(self) | {"display_name": self.person.name, "role": self.person.role, "verified": False}

    def describe(self) -> str:
        """Model-facing text, built only from config values and fixed enums (never free-form input)."""
        p = self.person
        if self.source == "voice_id":
            how = f"identified by voice (score {self.score:.2f})" if self.score is not None else "identified by voice"
        elif self.source == "declared":
            how = "selected on the dashboard or device"
        else:
            how = f"not identified ({self.reason or 'no voice sample'})"
            return (f"Speaker: unknown, {how}. Don't address them by name or record preferences as theirs. "
                    "Approval policy is unchanged.")
        return (f"Speaker: {p.name} ({p.role}), {how}. This is a label for context, not authentication: "
                "it grants no purchasing or approval authority; the server applies office policy.")


def save(conn, task_id: str, speaker: Speaker) -> None:
    """Stored in `task_speakers` (office/db.py)."""
    conn.execute("INSERT OR REPLACE INTO task_speakers VALUES (?, ?)", (task_id, json.dumps(asdict(speaker))))


def load(conn, task_id: str, person_name: str | None = None) -> Speaker:
    """The task's stored speaker; tasks created elsewhere (devices, rules) fall back to their person column."""
    row = conn.execute("SELECT speaker FROM task_speakers WHERE task_id = ?", (task_id,)).fetchone()
    return Speaker(**json.loads(row[0])) if row else Speaker.declared(person_name)


@dataclass
class SpeakerContext(AbstractCapability[Any]):
    """Tells the model who is speaking and hides speaker-gated tools from unidentified speakers."""

    speaker: Speaker = Speaker.unknown()
    id: str | None = "speaker"

    async def for_run(self, ctx: RunContext[Any]) -> "SpeakerContext":  # the run's own speaker
        return replace(self, speaker=ctx.deps.speaker)

    def get_instructions(self) -> InstructionPart:
        # Appended on change, so a resumed run with the same speaker keeps the cached prefix intact.
        return InstructionPart(self.speaker.describe(), name="speaker", on_change="append")

    async def prepare_tools(self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        if self.speaker.identified:
            return tool_defs
        return [t for t in tool_defs if (t.metadata or {}).get("speaker") != "identified"]
