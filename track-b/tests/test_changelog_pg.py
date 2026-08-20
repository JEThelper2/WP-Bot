"""Change log against a REAL Postgres (the pg-sandbox compose stack).

Skipped unless a Postgres is reachable. Spin one up with:

    docker compose -f track-b/pg-sandbox/docker-compose.yml up -d

then run with:

    WPBOT_PG_TEST_URL=postgres://wpbot:wpbot@localhost:5433/wpbot \
    pytest track-b/tests/test_changelog_pg.py -v
"""

import asyncio
import os

import pytest

from track_b.changelog import ChangeRow, PostgresChangeLog

PG_URL = os.environ.get("WPBOT_PG_TEST_URL", "")


def _pg_available() -> bool:
    if not PG_URL:
        return False
    try:
        import asyncpg

        async def probe():
            conn = await asyncpg.connect(PG_URL, timeout=5)
            await conn.close()

        asyncio.run(probe())
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="no Postgres sandbox running — see track-b/pg-sandbox/docker-compose.yml",
)


def run(coro):
    return asyncio.run(coro)


class _SyncLogWrapper:
    """Wrapper that creates a fresh connection pool for each async call."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _run(self, coro):
        return asyncio.run(coro)

    def record_change(self, row):
        async def _inner():
            store = await PostgresChangeLog.connect(self._dsn)
            try:
                return await store.record_change(row)
            finally:
                await store._pool.close()
        return self._run(_inner())

    def most_recent(self, owner_id):
        async def _inner():
            store = await PostgresChangeLog.connect(self._dsn)
            try:
                return await store.most_recent(owner_id)
            finally:
                await store._pool.close()
        return self._run(_inner())

    def get(self, change_id):
        async def _inner():
            store = await PostgresChangeLog.connect(self._dsn)
            try:
                return await store.get(change_id)
            finally:
                await store._pool.close()
        return self._run(_inner())


@pytest.fixture()
def log() -> _SyncLogWrapper:
    wrapper = _SyncLogWrapper(PG_URL)
    # Clean up any leftover data from previous runs
    async def _cleanup():
        store = await PostgresChangeLog.connect(PG_URL)
        try:
            async with store._pool.acquire() as conn:
                await conn.execute("DELETE FROM change_log")
        finally:
            await store._pool.close()
    asyncio.run(_cleanup())
    return wrapper


def make_row(change_id: str) -> ChangeRow:
    return ChangeRow(
        change_id=change_id,
        owner_id="15551234567",
        content_type="job",
        action="create",
        before=None,
        after={"title": "Barista", "content": "$18/hr", "post_id": 1},
        live_url="https://example.com/?p=1",
    )


def test_record_and_read_back(log):
    row = log.record_change(make_row("ch-test-1"))
    assert row.created_at is not None  # stamped by Postgres

    recent = log.most_recent("15551234567")
    assert recent.change_id == "ch-test-1"
    assert recent.content_type == "job"
    assert recent.before is None
    assert recent.after == {"title": "Barista", "content": "$18/hr", "post_id": 1}
    assert recent.live_url == "https://example.com/?p=1"

    by_id = log.get("ch-test-1")
    assert by_id is not None and by_id.owner_id == "15551234567"


def test_most_recent_returns_latest_per_owner(log):
    from datetime import datetime, timedelta, timezone

    # Use explicit timestamps to guarantee ordering across fresh pools
    now = datetime.now(tz=timezone.utc)
    log.record_change(ChangeRow(
        change_id="ch-old",
        owner_id="15551234567",
        content_type="job",
        action="create",
        before=None,
        after={"title": "Barista", "content": "$18/hr", "post_id": 1},
        live_url="https://example.com/?p=1",
        created_at=now - timedelta(seconds=10),
    ))
    log.record_change(ChangeRow(
        change_id="ch-new",
        owner_id="15551234567",
        content_type="job",
        action="update",
        before={"title": "Barista", "content": "$18/hr", "post_id": 1},
        after={"title": "Barista", "content": "$20/hr", "post_id": 1},
        created_at=now,
    ))
    log.record_change(ChangeRow(
        change_id="ch-other-owner",
        owner_id="99999999999",
        content_type="job",
        action="create",
        before=None,
        after={"title": "Barista", "content": "$18/hr", "post_id": 1},
        live_url="https://example.com/?p=1",
        created_at=now - timedelta(seconds=5),
    ))

    recent = log.most_recent("15551234567")
    assert recent.change_id == "ch-new"
