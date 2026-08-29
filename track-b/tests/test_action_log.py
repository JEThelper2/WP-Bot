"""Tests for the action log (§1.3) — undo support for the new state machine.

Covers both InMemoryActionLog (used by most tests) and SQLiteActionLog
(pilot-scale persistence).
"""

import asyncio
import pytest

from track_b.action_log import (
    ActionLogRow,
    InMemoryActionLog,
    SQLiteActionLog,
    init_action_log_db,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# InMemoryActionLog
# ---------------------------------------------------------------------------


class TestInMemoryActionLog:
    def test_record_and_retrieve(self):
        log = InMemoryActionLog()
        row = run(log.record(
            tenant_id="t1",
            action_type="menu_item_add",
            before_state=None,
            after_state={"id": 1, "title": "Jollof Rice"},
        ))
        assert row.id
        assert row.tenant_id == "t1"
        assert row.action_type == "menu_item_add"
        assert row.before_state is None
        assert row.after_state == {"id": 1, "title": "Jollof Rice"}
        assert row.undone_at is None
        assert row.source == "text"

    def test_most_recent(self):
        log = InMemoryActionLog()
        run(log.record(tenant_id="t1", action_type="menu_item_add",
                        before_state=None, after_state={"id": 1}))
        run(log.record(tenant_id="t1", action_type="menu_item_update",
                        before_state={"price": 100}, after_state={"price": 200}))
        run(log.record(tenant_id="t2", action_type="menu_item_add",
                        before_state=None, after_state={"id": 2}))

        recent = run(log.most_recent("t1"))
        assert recent is not None
        assert recent.action_type == "menu_item_update"

        assert run(log.most_recent("t2")).action_type == "menu_item_add"
        assert run(log.most_recent("nonexistent")) is None

    def test_mark_undone(self):
        log = InMemoryActionLog()
        row = run(log.record(
            tenant_id="t1", action_type="menu_item_add",
            before_state=None, after_state={"id": 1},
        ))
        assert run(log.mark_undone(row.id)) is True
        # Most recent should skip undone entries
        recent = run(log.most_recent("t1"))
        assert recent is None  # only entry is undone

    def test_list_recent(self):
        log = InMemoryActionLog()
        for i in range(7):
            run(log.record(
                tenant_id="t1", action_type="menu_item_add",
                before_state=None, after_state={"id": i},
            ))
        recent = run(log.list_recent("t1", limit=3))
        assert len(recent) == 3
        # Most recent first
        assert recent[0].after_state["id"] == 6

    def test_source_field(self):
        log = InMemoryActionLog()
        row = run(log.record(
            tenant_id="t1", action_type="menu_item_add",
            before_state=None, after_state={}, source="voice",
        ))
        assert row.source == "voice"


# ---------------------------------------------------------------------------
# SQLiteActionLog
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "action_log.db"
    init_action_log_db(path)
    return path


class TestSQLiteActionLog:
    def test_record_and_retrieve(self, db_path):
        log = SQLiteActionLog(db_path)
        row = run(log.record(
            tenant_id="t1", action_type="menu_item_add",
            before_state=None, after_state={"id": 1, "title": "Jollof Rice"},
        ))
        assert row.id
        assert row.action_type == "menu_item_add"
        assert row.after_state == {"id": 1, "title": "Jollof Rice"}

        recent = run(log.most_recent("t1"))
        assert recent is not None
        assert recent.id == row.id

    def test_most_recent_skips_undone(self, db_path):
        log = SQLiteActionLog(db_path)
        row1 = run(log.record(
            tenant_id="t1", action_type="menu_item_add",
            before_state=None, after_state={"id": 1},
        ))
        run(log.record(
            tenant_id="t1", action_type="menu_item_update",
            before_state={"price": 100}, after_state={"price": 200},
        ))
        run(log.mark_undone(row1.id))

        recent = run(log.most_recent("t1"))
        assert recent is not None
        assert recent.action_type == "menu_item_update"

    def test_mark_undone_persists(self, db_path):
        log = SQLiteActionLog(db_path)
        row = run(log.record(
            tenant_id="t1", action_type="menu_item_add",
            before_state=None, after_state={"id": 1},
        ))
        assert run(log.mark_undone(row.id)) is True
        assert run(log.mark_undone(row.id)) is False  # already undone

    def test_list_recent(self, db_path):
        log = SQLiteActionLog(db_path)
        for i in range(5):
            run(log.record(
                tenant_id="t1", action_type="menu_item_add",
                before_state=None, after_state={"id": i},
            ))
        recent = run(log.list_recent("t1", limit=2))
        assert len(recent) == 2

    def test_tenants_isolated(self, db_path):
        log = SQLiteActionLog(db_path)
        run(log.record(tenant_id="t1", action_type="menu_item_add",
                        before_state=None, after_state={"id": 1}))
        run(log.record(tenant_id="t2", action_type="menu_item_add",
                        before_state=None, after_state={"id": 2}))

        assert run(log.most_recent("t1")).after_state["id"] == 1
        assert run(log.most_recent("t2")).after_state["id"] == 2

    def test_undo_action_logged(self, db_path):
        log = SQLiteActionLog(db_path)
        # Original add
        original = run(log.record(
            tenant_id="t1", action_type="menu_item_add",
            before_state=None, after_state={"id": 1, "title": "Jollof"},
        ))
        # Mark undone
        run(log.mark_undone(original.id))
        # Log the undo itself
        undo_row = run(log.record(
            tenant_id="t1", action_type="undo",
            before_state={"id": 1, "title": "Jollof"},
            after_state={"deleted": True},
        ))
        assert undo_row.action_type == "undo"
        assert undo_row.before_state == {"id": 1, "title": "Jollof"}

        # Most recent is now the undo entry
        recent = run(log.most_recent("t1"))
        assert recent.action_type == "undo"
