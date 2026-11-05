import sqlite3
from dataclasses import dataclass

from office.config import Board, Person


@dataclass
class Deps:
    board: Board  # where the request came from
    person: Person  # who asked (voice ID or dashboard pick); GUEST if unknown
    task_id: str
    db: sqlite3.Connection
