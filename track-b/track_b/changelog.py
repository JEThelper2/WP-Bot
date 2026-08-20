"""Persistent change log (PRD §11).

Every successful WordPress write — after passing the B2/B3 gate — records
a row here: change_id, owner_id, content_type, action, the full `before`
and `after` state, timestamp, live_url, and (for undos) `undo_of`. Undo
is a reverse-apply of the stored `before` state, never a re-guess, so a
write that isn't logged is a failure state (see `apply_intent`).

`PostgresChangeLog` is the production implementation (asyncpg; the driver
is imported lazily so the rest of Track B works without it). The table:

    change_log (
        change_id    TEXT PRIMARY KEY,
        owner_id     TEXT NOT NULL,
        content_type TEXT NOT NULL,
        action       TEXT NOT NULL,        -- create | update | delete | undo
        before       JSONB,                -- full pre-write state
        after        JSONB,                -- full post-write state
        live_url     TEXT,
        undo_of      TEXT,                 -- change_id this row undoes
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    );

`InMemoryChangeLog` backs the unit tests with an injectable clock so the
24h undo window is deterministic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger("track_b.changelog")


@dataclass(frozen=True)
class ChangeRow:
    change_id: str
    owner_id: str
    content_type: str
    action: str  # create | update | delete | undo
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    live_url: str | None = None
    undo_of: str | None = None
    created_at: datetime | None = None  # stamped by the store

    @property
    def timestamp(self) -> float:
        ts = self.created_at
        if ts is None:
            return 0.0
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts.timestamp()


class ChangeLog(Protocol):
    async def record_change(self, row: ChangeRow) -> ChangeRow: ...

    async def most_recent(self, owner_id: str) -> ChangeRow | None: ...

    async def get(self, change_id: str) -> ChangeRow | None: ...

    async def list_changes(
        self,
        *,
        owner_id: str | None = None,
        content_type: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[ChangeRow]: ...

    async def count_by_action(self) -> dict[str, int]: ...

    async def count_failed(self) -> int: ...


class InMemoryChangeLog:
    """Deterministic in-memory log for tests/dev."""

    def __init__(self, time_fn: Any = None) -> None:
        import time as _time

        self._time = time_fn or _time.time
        self._rows: list[ChangeRow] = []
        self._seq = 0

    async def record_change(self, row: ChangeRow) -> ChangeRow:
        stamped = ChangeRow(
            change_id=row.change_id,
            owner_id=row.owner_id,
            content_type=row.content_type,
            action=row.action,
            before=row.before,
            after=row.after,
            live_url=row.live_url,
            undo_of=row.undo_of,
            created_at=datetime.fromtimestamp(self._time(), tz=UTC),
        )
        self._seq += 1
        self._rows.append((self._seq, stamped))
        return stamped

    async def most_recent(self, owner_id: str) -> ChangeRow | None:
        owned = [r for _, r in self._rows if r.owner_id == owner_id]
        if not owned:
            return None
        return max(owned, key=lambda r: (r.timestamp, self._order(r)))

    def _order(self, row: ChangeRow) -> int:
        for seq, stored in reversed(self._rows):
            if stored.change_id == row.change_id:
                return seq
        return 0

    async def get(self, change_id: str) -> ChangeRow | None:
        for _, row in self._rows:
            if row.change_id == change_id:
                return row
        return None

    async def list_changes(
        self,
        *,
        owner_id: str | None = None,
        content_type: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[ChangeRow]:
        rows = [r for _, r in self._rows]
        if owner_id:
            rows = [r for r in rows if r.owner_id == owner_id]
        if content_type:
            rows = [r for r in rows if r.content_type == content_type]
        if action:
            rows = [r for r in rows if r.action == action]
        rows.sort(key=lambda r: r.timestamp, reverse=True)
        return rows[:limit]

    async def count_by_action(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, row in self._rows:
            counts[row.action] = counts.get(row.action, 0) + 1
        return counts

    async def count_failed(self) -> int:
        return sum(1 for _, r in self._rows if r.action == "failed")

    def __len__(self) -> int:
        return len(self._rows)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS change_log (
    change_id    TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    action       TEXT NOT NULL,
    before       JSONB,
    after        JSONB,
    live_url     TEXT,
    undo_of      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_change_log_owner
    ON change_log (owner_id, created_at DESC);
"""


class PostgresChangeLog:
    """Production log backed by Postgres (asyncpg, lazy import)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> PostgresChangeLog:
        import asyncpg

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        async with pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        return cls(pool)

    async def record_change(self, row: ChangeRow) -> ChangeRow:
        async with self._pool.acquire() as conn:
            created_at = await conn.fetchval(
                """
                INSERT INTO change_log
                    (change_id, owner_id, content_type, action,
                     before, after, live_url, undo_of)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING created_at
                """,
                row.change_id,
                row.owner_id,
                row.content_type,
                row.action,
                _json(row.before),
                _json(row.after),
                row.live_url,
                row.undo_of,
            )
        return ChangeRow(
            change_id=row.change_id,
            owner_id=row.owner_id,
            content_type=row.content_type,
            action=row.action,
            before=row.before,
            after=row.after,
            live_url=row.live_url,
            undo_of=row.undo_of,
            created_at=created_at,
        )

    async def most_recent(self, owner_id: str) -> ChangeRow | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM change_log
                WHERE owner_id = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                owner_id,
            )
        return _row_from_record(row) if row else None

    async def get(self, change_id: str) -> ChangeRow | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM change_log WHERE change_id = $1", change_id)
        return _row_from_record(row) if row else None


def _json(value: Any) -> Any:
    return value if value is None else __import__("json").dumps(value)


def _row_from_record(record: Any) -> ChangeRow:
    import json

    def loads(v: Any) -> dict | None:
        if v is None:
            return None
        return json.loads(v) if isinstance(v, str) else dict(v)

    return ChangeRow(
        change_id=record["change_id"],
        owner_id=record["owner_id"],
        content_type=record["content_type"],
        action=record["action"],
        before=loads(record["before"]),
        after=loads(record["after"]),
        live_url=record["live_url"],
        undo_of=record["undo_of"],
        created_at=record["created_at"],
    )
