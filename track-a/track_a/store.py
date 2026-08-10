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
- `media_ref` is the media id Track A later uses to download the voice
  note via Meta's Media API.
- `message_text` is the normalized, channel-agnostic text (raw body for
  text messages, Whisper transcript for voice notes). Intent parsing
  (next milestone) reads only this field — rows without one are skipped.
- `processing_status` tracks pipeline state: new | text | transcribed |
  low_confidence | failed | unsupported.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    wam_id             TEXT UNIQUE NOT NULL,
    owner_phone        TEXT NOT NULL,
    message_type       TEXT NOT NULL,
    content            TEXT,
    media_ref          TEXT,
    meta_timestamp     TEXT,
    received_at        TEXT NOT NULL,
    message_text       TEXT,
    processing_status  TEXT NOT NULL DEFAULT 'new'
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns added after the original schema (dev-stage migration)."""
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(inbound_messages)")
    }
    if "message_text" not in existing:
        conn.execute("ALTER TABLE inbound_messages ADD COLUMN message_text TEXT")
    if "processing_status" not in existing:
        conn.execute(
            "ALTER TABLE inbound_messages "
            "ADD COLUMN processing_status TEXT NOT NULL DEFAULT 'new'"
        )


def init_db(db_path: Path) -> None:
    """Create the table (idempotent). Also creates the parent directory."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(_SCHEMA)
        _migrate(conn)


def insert_message(
    db_path: Path,
    *,
    wam_id: str,
    owner_phone: str,
    message_type: str,
    content: str | None,
    media_ref: str | None,
    meta_timestamp: str | None,
) -> int | None:
    """Persist one inbound message.

    Returns the new row id, or None if it was a duplicate delivery
    (same wam_id already logged).
    """
    received_at = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as conn:
        try:
            cur = conn.execute(
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
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None


def get_message(db_path: Path, row_id: int) -> dict | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM inbound_messages WHERE id = ?", (row_id,)
        ).fetchone()
    return dict(row) if row else None


def update_processing(
    db_path: Path, row_id: int, *, status: str, message_text: str | None
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE inbound_messages SET processing_status = ?, message_text = ? "
            "WHERE id = ?",
            (status, message_text, row_id),
        )


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
