import sqlite3
from dataclasses import dataclass
from typing import Any

from office.config import Board, Person


@dataclass
class Deps:
    board: Board  # where the request came from
    person: Person  # who asked (voice ID or dashboard pick); GUEST if unknown
    task_id: str
    db: sqlite3.Connection
    rooms: Any = None
    event_id: str | None = None
    conversation_id: str | None = None
    prompt: str = ""
    room_used: bool = False
    room_task_id: str | None = None
    procurement_task_id: str | None = None
