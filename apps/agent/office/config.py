"""Static office config (boards, people, catalogue) and env settings. Edit the JSON, not this file."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")

MODEL = os.getenv("OFFICE_MODEL", "mock")  # "mock" = offline scripted model; e.g. "anthropic:claude-sonnet-5-5"
LIVE = MODEL != "mock"  # mock mode is fully offline: no Notion, local brain copy only
DATA = ROOT / ".data"  # gitignored: runtime DB, memory, OAuth tokens, Notion snapshot
MEMORY_PATH = os.getenv("MEMORY_PATH", str(DATA / "memory.sqlite"))
DATABASE_PATH = os.getenv("AGENT_DATABASE_PATH", str(ROOT / ".data" / "agent.sqlite"))
if DATABASE_PATH != ":memory:":
    DATABASE_PATH = str(ROOT / DATABASE_PATH)


def _load(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


@dataclass
class Board:
    id: str
    location: str
    responsible_team: str
    equipment: list[str]
    modules: set[str]
    triggers: list[dict] = field(default_factory=list)


@dataclass
class Person:
    name: str
    role: str
    auto_approve_nok: int


GUEST = Person(name="unknown speaker", role="guest", auto_approve_nok=0)

BOARDS: dict[str, Board] = {
    b["id"]: Board(**{**b, "modules": set(b["modules"])}) for b in _load("config/boards.json")
}
PEOPLE: dict[str, Person] = {p["name"].lower(): Person(**p) for p in _load("config/people.json")}
PRODUCTS: dict[str, dict] = {p["id"]: p for p in _load("fixtures/products.json")}
OFFICE: dict = _load("config/office.json")


def person(name: str | None) -> Person:
    return PEOPLE.get((name or "").lower(), GUEST)
