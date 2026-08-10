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


@pytest.fixture()
async def log() -> PostgresChangeLog:
    store = await PostgresChangeLog.connect(PG_URL)
    yield store
    await store._pool.close()


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
    row = run(log.record_change(make_row("ch-test-1")))
    assert row.created_at is not None  # stamped by Postgres

    recent = run(log.most_recent("15551234567"))
    assert recent.change_id == "ch-test-1"
    assert recent.content_type == "job"
    assert recent.before is None
    assert recent.after == {"title": "Barista", "content": "$18/hr", "post_id": 1}
    assert recent.live_url == "https://example.com/?p=1"

    by_id = run(log.get("ch-test-1"))
    assert by_id is not None and by_id.owner_id == "15551234567"


def test_most_recent_returns_latest_per_owner(log):
    run(log.record_change(make_row("ch-old")))
    run(
        log.record_change(
            ChangeRow(
                change_id="ch-new",
                owner_id="15551234567",
                content_type="job",
                action="update",
                before={"title": "Barista", "content": "$18/hr", "post_id": 1},
                after={"title": "Barista", "content": "$20/hr", "post_id": 1},
            )
        )
    )
    run(log.record_change(make_row("ch-other-owner")))  # different owner

    recent = run(log.most_recent("15551234567"))
    assert recent.change_id == "ch-new"
