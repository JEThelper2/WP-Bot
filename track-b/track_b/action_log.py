"""Action log for undo support (PRODUCTION_SPEC_DETAILED.md §1.3).

Extends the existing change_log with spec-required fields:
- undone_at: NULL unless reverted
- source: 'text' | 'voice' | 'image'

The existing `changelog.py` module continues to work for the current
integration tests. This module provides the spec-aligned version that
the new state machine (Phase 3) will use.

Both InMemoryActionLog (tests) and the Postgres implementation follow
the same ActionLog protocol, matching the existing ChangeLog pattern.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger("track_b.action_log")


@dataclass(frozen=True)
class ActionLogRow:
    """One entry in the action log (§1.3)."""

    id: str  # UUID
    tenant_id: str
    action_type: str  # menu_item_add, menu_item_update, menu_item_delete,
                       # business_info_update, page_content_update, undo
    before_state: dict[str, Any] | None
    after_state: dict[str, Any]
    executed_at: datetime
    undone_at: datetime | None = None
    source: str = "text"  # text | voice | image

    @property
    def timestamp(self) -> float:
        return self.executed_at.timestamp()


class ActionLog(Protocol):
    async def record(
        self,
        *,
        tenant_id: str,
        action_type: str,
        before_state: dict[str, Any] | None,
        after_state: dict[str, Any],
        source: str = "text",
    ) -> ActionLogRow: ...

    async def most_recent(self, tenant_id: str) -> ActionLogRow | None: ...

    async def mark_undone(self, action_id: str) -> bool: ...

    async def list_recent(
        self, tenant_id: str, *, limit: int = 5
    ) -> list[ActionLogRow]: ...


# ---------------------------------------------------------------------------
# In-memory implementation (tests / dev)
# ---------------------------------------------------------------------------

class InMemoryActionLog:
    """Deterministic in-memory log for tests."""

    def __init__(self) -> None:
        self._rows: list[tuple[int, ActionLogRow]] = []
        self._seq = 0

    async def record(
        self,
        *,
        tenant_id: str,
        action_type: str,
        before_state: dict[str, Any] | None,
        after_state: dict[str, Any],
        source: str = "text",
    ) -> ActionLogRow:
        row = ActionLogRow(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            action_type=action_type,
            before_state=before_state,
            after_state=after_state,
            executed_at=datetime.now(UTC),
            source=source,
        )
        self._seq += 1
        self._rows.append((self._seq, row))
        return row

    async def most_recent(self, tenant_id: str) -> ActionLogRow | None:
        owned = [r for _, r in self._rows if r.tenant_id == tenant_id and r.undone_at is None]
        if not owned:
            return None
        return max(owned, key=lambda r: (r.timestamp, self._order(r)))

    async def mark_undone(self, action_id: str) -> bool:
        for seq, row in self._rows:
            if row.id == action_id:
                # Frozen dataclass — replace in list
                updated = ActionLogRow(
                    id=row.id,
                    tenant_id=row.tenant_id,
                    action_type=row.action_type,
                    before_state=row.before_state,
                    after_state=row.after_state,
                    executed_at=row.executed_at,
                    undone_at=datetime.now(UTC),
                    source=row.source,
                )
                idx = self._rows.index((seq, row))
                self._rows[idx] = (seq, updated)
                return True
        return False

    async def list_recent(self, tenant_id: str, *, limit: int = 5) -> list[ActionLogRow]:
        owned = [r for _, r in self._rows if r.tenant_id == tenant_id]
        owned.sort(key=lambda r: r.timestamp, reverse=True)
        return owned[:limit]

    def _order(self, row: ActionLogRow) -> int:
        for seq, stored in self._rows:
            if stored.id == row.id:
                return seq
        return 0


# ---------------------------------------------------------------------------
# SQLite implementation (pilot scale)
# ---------------------------------------------------------------------------

_ACTION_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS action_log (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    before_state    TEXT,                -- JSON or NULL
    after_state     TEXT NOT NULL,       -- JSON
    executed_at     TEXT NOT NULL,       -- ISO timestamp
    undone_at       TEXT,                -- ISO timestamp or NULL
    source          TEXT NOT NULL DEFAULT 'text'
);
CREATE INDEX IF NOT EXISTS idx_action_log_tenant
    ON action_log(tenant_id, executed_at DESC);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _connection(db_path: Path):
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_action_log_db(db_path: Path) -> None:
    """Create the action_log table (idempotent)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connection(db_path) as conn:
        conn.executescript(_ACTION_LOG_SCHEMA)


class SQLiteActionLog:
    """SQLite-backed action log for pilot scale."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        init_action_log_db(db_path)

    async def record(
        self,
        *,
        tenant_id: str,
        action_type: str,
        before_state: dict[str, Any] | None,
        after_state: dict[str, Any],
        source: str = "text",
    ) -> ActionLogRow:
        row_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        before_json = json.dumps(before_state) if before_state is not None else None
        after_json = json.dumps(after_state)

        with _connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO action_log
                    (id, tenant_id, action_type, before_state, after_state,
                     executed_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (row_id, tenant_id, action_type, before_json, after_json, now, source),
            )

        return ActionLogRow(
            id=row_id,
            tenant_id=tenant_id,
            action_type=action_type,
            before_state=before_state,
            after_state=after_state,
            executed_at=datetime.fromisoformat(now),
            source=source,
        )

    async def most_recent(self, tenant_id: str) -> ActionLogRow | None:
        with _connection(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM action_log
                WHERE tenant_id = ? AND undone_at IS NULL
                ORDER BY executed_at DESC LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()
        return _row_to_action_log(row) if row else None

    async def mark_undone(self, action_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with _connection(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE action_log SET undone_at = ? WHERE id = ? AND undone_at IS NULL",
                (now, action_id),
            )
        return cur.rowcount > 0

    async def list_recent(self, tenant_id: str, *, limit: int = 5) -> list[ActionLogRow]:
        with _connection(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM action_log
                WHERE tenant_id = ?
                ORDER BY executed_at DESC LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
        return [_row_to_action_log(r) for r in rows]


def _row_to_action_log(row: sqlite3.Row) -> ActionLogRow:
    before = json.loads(row["before_state"]) if row["before_state"] else None
    after = json.loads(row["after_state"])
    return ActionLogRow(
        id=row["id"],
        tenant_id=row["tenant_id"],
        action_type=row["action_type"],
        before_state=before,
        after_state=after,
        executed_at=datetime.fromisoformat(row["executed_at"]),
        undone_at=datetime.fromisoformat(row["undone_at"]) if row["undone_at"] else None,
        source=row["source"],
    )
