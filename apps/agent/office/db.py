"""SQLite: one connection, plain SQL, schema created at startup. No ORM, no migrations."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from office import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, board_id TEXT NOT NULL, person TEXT NOT NULL, prompt TEXT NOT NULL,
  status TEXT NOT NULL,            -- running | awaiting_approval | done | failed
  reply TEXT, messages TEXT NOT NULL DEFAULT '[]', pending TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, tool_call_id TEXT NOT NULL UNIQUE,
  product_id TEXT NOT NULL, quantity INTEGER NOT NULL, total_nok INTEGER NOT NULL,
  status TEXT NOT NULL,            -- simulated | delivered
  ref TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, board_id TEXT NOT NULL, summary TEXT NOT NULL,
  team TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sensor_readings (board_id TEXT, kind TEXT, value REAL, at TEXT);
CREATE TABLE IF NOT EXISTS board_commands (board_id TEXT PRIMARY KEY, text TEXT, color TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS device_events (event_id TEXT PRIMARY KEY, task_id TEXT, response TEXT);
-- office/speaker.py: who spoke and on what evidence (JSON). Informational; never authority.
CREATE TABLE IF NOT EXISTS task_speakers (task_id TEXT PRIMARY KEY, speaker TEXT NOT NULL);
-- office/approval_email.py: at most one approval-request email per (task, proposal). Notification only.
CREATE TABLE IF NOT EXISTS approval_emails (
  task_id TEXT NOT NULL, fingerprint TEXT NOT NULL,  -- sha256 of the exact proposal fingerprint(s) emailed
  approver TEXT NOT NULL, state TEXT NOT NULL,       -- skipped | sending | sent | failed | uncertain
  detail TEXT, message_id TEXT, idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (task_id, fingerprint)
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def connect(path: str = config.DATABASE_PATH) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def row(conn, sql, *args) -> dict | None:
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def rows(conn, sql, *args) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def task_view(t: dict) -> dict:
    """Task as the API/dashboard sees it (no raw model messages)."""
    return {k: v for k, v in t.items() if k != "messages"} | {"pending": json.loads(t["pending"])}


def set_command(conn, board_id: str, text: str, color: str) -> dict:
    conn.execute(
        "INSERT OR REPLACE INTO board_commands VALUES (?, ?, ?, ?)", (board_id, text[:120], color, now())
    )
    return {"station_id": board_id, "text": text[:120], "color": color}
