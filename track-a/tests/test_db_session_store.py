"""Tests for DBSessionStore (§1.2 conversation_sessions table).

Verifies that the DB-backed session store has the same interface as
the in-memory SessionStore and correctly persists sessions to SQLite.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from track_a.session import DBSessionStore, SessionState, SessionStore
from track_a.tenant_store import (
    create_tenant,
    get_session,
    init_tenant_db,
    upsert_session,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OWNER = "15551234567"
OWNER2 = "15559999999"


def _setup_tenant(db_path: Path, sender_id: str = OWNER) -> dict:
    """Create a tenant and return the row."""
    init_tenant_db(db_path)
    tenant = create_tenant(
        db_path,
        sender_id=sender_id,
        business_name="Test Business",
        wp_site_url="https://test.example.com",
        wp_app_username="editor",
        wp_app_password_enc="test-password",
    )
    return tenant


# ---------------------------------------------------------------------------
# Basic get/set/clear
# ---------------------------------------------------------------------------

class TestDBSessionStoreBasic:
    def test_get_returns_none_for_unknown_owner(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)
        assert store.get("nonexistent") is None

    def test_get_returns_none_for_no_tenant(self, tmp_path):
        db = tmp_path / "test.db"
        init_tenant_db(db)
        store = DBSessionStore(db)
        assert store.get(OWNER) is None

    def test_set_then_get(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)

        state = SessionState(state="AWAITING_CONFIRMATION", pending_intent={"action": "delete"})
        store.set(OWNER, state)

        got = store.get(OWNER)
        assert got is not None
        assert got.state == "AWAITING_CONFIRMATION"
        assert got.pending_intent == {"action": "delete"}

    def test_set_then_clear(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)

        store.set(OWNER, SessionState(state="AWAITING_CLARIFICATION"))
        assert store.get(OWNER) is not None

        store.clear(OWNER)
        assert store.get(OWNER) is None

    def test_clear_nonexistent_is_noop(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)
        store.clear(OWNER)  # should not raise

    def test_set_overwrites_existing(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)

        store.set(OWNER, SessionState(state="IDLE"))
        store.set(OWNER, SessionState(state="AWAITING_CONFIRMATION", turns=2))

        got = store.get(OWNER)
        assert got.state == "AWAITING_CONFIRMATION"
        assert got.turns == 2


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

class TestDBSessionStoreSerialization:
    def test_pending_intent_round_trip(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)

        intent = {"action": "delete", "content_type": "job", "fields": {"title": "Cashier"}}
        state = SessionState(state="AWAITING_CONFIRMATION", pending_intent=intent)
        store.set(OWNER, state)

        got = store.get(OWNER)
        assert got.pending_intent == intent

    def test_extra_fields_round_trip(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)

        state = SessionState(
            state="AWAITING_CLARIFICATION",
            asked_field="title",
            turns=3,
            re_ask_count=1,
            site_id="site-42",
            voice_transcript="add jollof rice",
            voice_confidence=0.85,
            source="voice",
            original_message="add jollof rice",
        )
        store.set(OWNER, state)

        got = store.get(OWNER)
        assert got.asked_field == "title"
        assert got.turns == 3
        assert got.re_ask_count == 1
        assert got.site_id == "site-42"
        assert got.voice_transcript == "add jollof rice"
        assert got.voice_confidence == 0.85
        assert got.source == "voice"
        assert got.original_message == "add jollof rice"

    def test_context_history_round_trip(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)

        history = [
            {"role": "owner", "text": "add jollof rice", "at": "2026-01-01T00:00:00Z"},
            {"role": "assistant", "text": "What price?", "at": "2026-01-01T00:00:01Z"},
        ]
        state = SessionState(state="AWAITING_CLARIFICATION", context_history=history)
        store.set(OWNER, state)

        got = store.get(OWNER)
        assert got.context_history == history

    def test_exchange_falls_back_to_context_history(self, tmp_path):
        """exchange field should use context_history when not explicitly set."""
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)

        history = [{"role": "owner", "text": "hello", "at": "2026-01-01T00:00:00Z"}]
        state = SessionState(state="AWAITING_CLARIFICATION", context_history=history)
        store.set(OWNER, state)

        got = store.get(OWNER)
        assert got.exchange == history

    def test_empty_state_round_trip(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db)
        store = DBSessionStore(db)

        state = SessionState(state="IDLE")
        store.set(OWNER, state)

        got = store.get(OWNER)
        assert got.state == "IDLE"
        assert got.pending_intent is None
        assert got.asked_field is None
        assert got.turns == 0


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

class TestDBSessionStoreIsolation:
    def test_two_owners_independent(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db, OWNER)
        _setup_tenant(db, OWNER2)
        store = DBSessionStore(db)

        store.set(OWNER, SessionState(state="AWAITING_CONFIRMATION"))
        store.set(OWNER2, SessionState(state="AWAITING_CLARIFICATION"))

        assert store.get(OWNER).state == "AWAITING_CONFIRMATION"
        assert store.get(OWNER2).state == "AWAITING_CLARIFICATION"

    def test_clear_one_does_not_affect_other(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db, OWNER)
        _setup_tenant(db, OWNER2)
        store = DBSessionStore(db)

        store.set(OWNER, SessionState(state="AWAITING_CONFIRMATION"))
        store.set(OWNER2, SessionState(state="IDLE"))

        store.clear(OWNER)
        assert store.get(OWNER) is None
        assert store.get(OWNER2).state == "IDLE"


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

class TestDBSessionStoreExpiry:
    def test_expired_session_returns_none(self, tmp_path):
        db = tmp_path / "test.db"
        tenant = _setup_tenant(db)
        store = DBSessionStore(db, ttl_minutes=15)

        # Manually insert an expired session
        import sqlite3
        past = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO conversation_sessions (id, tenant_id, state, pending_intent, context_history, last_message_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("test-id", tenant["id"], "AWAITING_CONFIRMATION", None, "[]", past, past),
        )
        conn.commit()
        conn.close()

        # DBSessionStore.get calls get_session which checks expiry
        assert store.get(OWNER) is None


# ---------------------------------------------------------------------------
# __len__
# ---------------------------------------------------------------------------

class TestDBSessionStoreLen:
    def test_len_counts_active_sessions(self, tmp_path):
        db = tmp_path / "test.db"
        _setup_tenant(db, OWNER)
        _setup_tenant(db, OWNER2)
        store = DBSessionStore(db)

        assert len(store) == 0

        store.set(OWNER, SessionState(state="IDLE"))
        assert len(store) == 1

        store.set(OWNER2, SessionState(state="AWAITING_CLARIFICATION"))
        assert len(store) == 2

        store.clear(OWNER)
        assert len(store) == 1


# ---------------------------------------------------------------------------
# Interface compatibility with SessionStore
# ---------------------------------------------------------------------------

class TestDBSessionStoreInterface:
    """Verify that DBSessionStore has the same public API as SessionStore."""

    def test_has_get_set_clear_cleanup(self):
        import inspect
        db_methods = {m for m in dir(DBSessionStore) if not m.startswith("_")}
        inmem_methods = {m for m in dir(SessionStore) if not m.startswith("_")}
        # DBSessionStore should have at least: get, set, clear, cleanup
        for method in ("get", "set", "clear", "cleanup"):
            assert method in db_methods, f"DBSessionStore missing {method}"

    def test_get_signature_matches(self):
        import inspect
        db_sig = inspect.signature(DBSessionStore.get)
        inmem_sig = inspect.signature(SessionStore.get)
        assert list(db_sig.parameters) == list(inmem_sig.parameters)
