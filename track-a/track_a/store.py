"""Lightweight persistence for inbound WhatsApp messages.

A single SQLite table for now — this is the Track A message log, not the
Track B change-log system (that lives on the WordPress side later). One
connection per operation keeps things simple and thread-safe enough for
this stage.

Design notes:
- `wam_id` (Meta's message id) is UNIQUE: Meta redelivers webhooks, so
  duplicate deliveries are skipped, not double-logged.
- `content` holds the raw text body for text messages, or a JSON dump of
  the media metadata (id, mime_type, ...) for audio and other types.
- `media_ref` is the media id Track A will later use to download the
  voice note via Meta's Media API (transcription comes in a later step).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wam_id          TEXT UNIQUE NOT NULL,
    owner_phone     TEXT NOT NULL,
    message_type    TEXT NOT NULL,
    content         TEXT,
    media_ref       TEXT,
    meta_timestamp  TEXT,
    received_at     TEXT NOT NULL
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    """Create the table (idempotent). Also creates the parent directory."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(_SCHEMA)


def insert_message(
    db_path: Path,
    *,
    wam_id: str,
    owner_phone: str,
    message_type: str,
    content: str | None,
    media_ref: str | None,
    meta_timestamp: str | None,
) -> bool:
    """Persist one inbound message.

    Returns True if inserted, False if it was a duplicate delivery
    (same wam_id already logged).
    """
    received_at = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        try:
            conn.execute(
                """
                INSERT INTO inbound_messages
                    (wam_id, owner_phone, message_type, content,
                     media_ref, meta_timestamp, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    wam_id,
                    owner_phone,
                    message_type,
                    content,
                    media_ref,
                    meta_timestamp,
                    received_at,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def list_messages(db_path: Path, limit: int = 100) -> list[dict]:
    """Return the most recent logged messages, newest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM inbound_messages
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_messages(db_path: Path) -> int:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM inbound_messages").fetchone()
    return int(row["n"])


def _content_json(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)
