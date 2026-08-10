"""In-memory change log: stamping, per-owner ordering, lookup."""

import asyncio

from track_b.changelog import ChangeRow, InMemoryChangeLog


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_row(change_id, owner="owner-1", action="create", after=None):
    return ChangeRow(
        change_id=change_id,
        owner_id=owner,
        content_type="job",
        action=action,
        before=None,
        after=after or {"title": "X", "post_id": 1},
    )


def test_record_stamps_created_at():
    clock = Clock()
    log = InMemoryChangeLog(time_fn=clock.time)
    stamped = run(log.record_change(make_row("ch-1")))
    assert stamped.created_at is not None
    assert stamped.timestamp == clock.now


def test_most_recent_is_latest_per_owner():
    clock = Clock()
    log = InMemoryChangeLog(time_fn=clock.time)
    run(log.record_change(make_row("ch-1", owner="alice")))
    clock.advance(10)
    run(log.record_change(make_row("ch-2", owner="alice")))
    clock.advance(10)
    run(log.record_change(make_row("ch-3", owner="bob")))

    assert run(log.most_recent("alice")).change_id == "ch-2"
    assert run(log.most_recent("bob")).change_id == "ch-3"
    assert run(log.most_recent("nobody")) is None


def test_get_by_change_id():
    log = InMemoryChangeLog()
    run(log.record_change(make_row("ch-abc", owner="alice")))
    row = run(log.get("ch-abc"))
    assert row is not None and row.owner_id == "alice"
    assert run(log.get("ch-missing")) is None
