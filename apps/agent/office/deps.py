import sqlite3
from dataclasses import dataclass

from office.config import Board, Person
from office.speaker import Speaker


@dataclass
class Deps:
    board: Board  # where the request came from
    person: Person  # speaker.person: who asked (voice ID or dashboard pick); GUEST if unknown
    task_id: str
    db: sqlite3.Connection
    speaker: Speaker = Speaker.unknown()  # who spoke and on what evidence; never authority
