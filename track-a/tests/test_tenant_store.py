"""Tests for the new multi-tenant tables (§1.1–1.5).

Covers: tenants CRUD, conversation_sessions lifecycle, processed_messages
idempotency, and rate_limit_buckets fixed-window counter.
"""

import pytest

from track_a.tenant_store import (
    check_and_increment_rate_limit,
    cleanup_expired_sessions,
    clear_session,
    create_tenant,
    get_session,
    get_tenant,
    get_tenant_by_sender,
    init_tenant_db,
    mark_message_processed,
    purge_old_processed_messages,
    set_tenant_onboarded,
    update_tenant_credentials,
    update_tenant_status,
    upsert_session,
)


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "tenants.db"
    init_tenant_db(path)
    return path


# -------------------------------------------------------------------
# Tenants (§1.1)
# -------------------------------------------------------------------


class TestTenants:
    def test_create_and_get(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678", business_name="Mama Nkechi")
        assert tenant["id"]
        assert tenant["sender_id"] == "2348012345678"
        assert tenant["business_name"] == "Mama Nkechi"
        assert tenant["status"] == "onboarding"

        fetched = get_tenant(db_path, tenant["id"])
        assert fetched is not None
        assert fetched["sender_id"] == "2348012345678"

    def test_get_by_sender(self, db_path):
        create_tenant(db_path, sender_id="2348012345678")
        create_tenant(db_path, sender_id="2348098765432")

        found = get_tenant_by_sender(db_path, "2348098765432")
        assert found is not None
        assert found["sender_id"] == "2348098765432"

        assert get_tenant_by_sender(db_path, "nonexistent") is None

    def test_sender_id_is_unique(self, db_path):
        create_tenant(db_path, sender_id="2348012345678")
        with pytest.raises(Exception):  # UNIQUE constraint
            create_tenant(db_path, sender_id="2348012345678")

    def test_update_status(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        assert update_tenant_status(db_path, tenant["id"], "degraded")
        assert get_tenant(db_path, tenant["id"])["status"] == "degraded"
        assert not update_tenant_status(db_path, "nonexistent", "active")

    def test_set_onboarded(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        assert set_tenant_onboarded(db_path, tenant["id"])
        t = get_tenant(db_path, tenant["id"])
        assert t["status"] == "active"
        assert t["onboarded_at"] is not None

    def test_update_credentials(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        assert update_tenant_credentials(
            db_path,
            tenant["id"],
            wp_site_url="https://mamanchi.com",
            wp_app_username="editor",
            wp_app_password_enc="enc_abc123",
            business_name="Mama Nkechi Restaurant",
        )
        t = get_tenant(db_path, tenant["id"])
        assert t["wp_site_url"] == "https://mamanchi.com"
        assert t["wp_app_username"] == "editor"
        assert t["business_name"] == "Mama Nkechi Restaurant"


# -------------------------------------------------------------------
# Conversation sessions (§1.2)
# -------------------------------------------------------------------


class TestConversationSessions:
    def test_upsert_creates_session(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        session = upsert_session(db_path, tenant["id"], state="AWAITING_CLARIFICATION")
        assert session is not None
        assert session["state"] == "AWAITING_CLARIFICATION"
        assert session["tenant_id"] == tenant["id"]

    def test_get_session_returns_none_when_expired(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        upsert_session(db_path, tenant["id"], state="IDLE", ttl_minutes=0)
        # ttl=0 means expires immediately
        session = get_session(db_path, tenant["id"])
        assert session is None

    def test_upsert_updates_existing(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        upsert_session(db_path, tenant["id"], state="IDLE")
        upsert_session(db_path, tenant["id"], state="AWAITING_CONFIRMATION")

        sessions = _count_sessions(db_path, tenant["id"])
        assert sessions == 1  # only one row per tenant

        session = get_session(db_path, tenant["id"])
        assert session["state"] == "AWAITING_CONFIRMATION"

    def test_clear_session(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        upsert_session(db_path, tenant["id"], state="IDLE")
        clear_session(db_path, tenant["id"])
        assert get_session(db_path, tenant["id"]) is None

    def test_context_history_persisted(self, db_path):
        import json
        tenant = create_tenant(db_path, sender_id="2348012345678")
        history = json.dumps([
            {"role": "owner", "text": "change the price"},
            {"role": "bot", "text": "What should the new price be?"},
        ])
        upsert_session(
            db_path, tenant["id"],
            state="AWAITING_CLARIFICATION",
            context_history=history,
        )
        session = get_session(db_path, tenant["id"])
        assert json.loads(session["context_history"]) == [
            {"role": "owner", "text": "change the price"},
            {"role": "bot", "text": "What should the new price be?"},
        ]

    def test_cleanup_expired(self, db_path):
        tenant1 = create_tenant(db_path, sender_id="111")
        tenant2 = create_tenant(db_path, sender_id="222")
        upsert_session(db_path, tenant1["id"], state="IDLE", ttl_minutes=0)  # expires immediately
        upsert_session(db_path, tenant2["id"], state="IDLE", ttl_minutes=60)

        # Note: get_session already does lazy cleanup of expired sessions,
        # so cleanup_expired_sessions is for eager bulk sweep.
        # After get_session on tenant1, the expired row is already deleted.
        get_session(db_path, tenant1["id"])  # triggers lazy cleanup
        removed = cleanup_expired_sessions(db_path)
        assert removed == 0  # already cleaned up lazily
        assert get_session(db_path, tenant1["id"]) is None
        assert get_session(db_path, tenant2["id"]) is not None


def _count_sessions(db_path, tenant_id):
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT COUNT(*) FROM conversation_sessions WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    conn.close()
    return row[0]


# -------------------------------------------------------------------
# Processed messages — idempotency (§1.4)
# -------------------------------------------------------------------


class TestProcessedMessages:
    def test_first_message_accepted(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        assert mark_message_processed(db_path, tenant["id"], "wamid.abc123") is True

    def test_duplicate_message_rejected(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        assert mark_message_processed(db_path, tenant["id"], "wamid.abc123") is True
        assert mark_message_processed(db_path, tenant["id"], "wamid.abc123") is False

    def test_different_messages_accepted(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        assert mark_message_processed(db_path, tenant["id"], "wamid.abc") is True
        assert mark_message_processed(db_path, tenant["id"], "wamid.def") is True

    def test_different_tenants_independent(self, db_path):
        t1 = create_tenant(db_path, sender_id="111")
        t2 = create_tenant(db_path, sender_id="222")
        assert mark_message_processed(db_path, t1["id"], "wamid.same") is True
        assert mark_message_processed(db_path, t2["id"], "wamid.same") is True

    def test_purge_old_messages(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        mark_message_processed(db_path, tenant["id"], "wamid.old")
        # Purge with 0 days removes everything
        removed = purge_old_processed_messages(db_path, days=0)
        assert removed >= 1


# -------------------------------------------------------------------
# Rate limit buckets (§1.5)
# -------------------------------------------------------------------


class TestRateLimitBuckets:
    def test_first_message_not_limited(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        limited, count = check_and_increment_rate_limit(db_path, tenant["id"])
        assert limited is False
        assert count == 1

    def test_under_limit_not_limited(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        for i in range(29):
            limited, count = check_and_increment_rate_limit(db_path, tenant["id"], max_messages=30)
            assert limited is False
        # 30th message — still OK
        limited, count = check_and_increment_rate_limit(db_path, tenant["id"], max_messages=30)
        assert limited is False
        assert count == 30

    def test_over_limit_is_limited(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        for i in range(30):
            check_and_increment_rate_limit(db_path, tenant["id"], max_messages=30)
        # 31st message — limited
        limited, count = check_and_increment_rate_limit(db_path, tenant["id"], max_messages=30)
        assert limited is True
        assert count == 31

    def test_window_reset(self, db_path):
        tenant = create_tenant(db_path, sender_id="2348012345678")
        # Fill the bucket
        for i in range(30):
            check_and_increment_rate_limit(db_path, tenant["id"], max_messages=30)
        # Window=0 hours means every call resets
        limited, count = check_and_increment_rate_limit(
            db_path, tenant["id"], max_messages=30, window_hours=0
        )
        assert limited is False
        assert count == 1

    def test_tenants_independent(self, db_path):
        t1 = create_tenant(db_path, sender_id="111")
        t2 = create_tenant(db_path, sender_id="222")
        for i in range(30):
            check_and_increment_rate_limit(db_path, t1["id"], max_messages=30)
        # t1 is limited, t2 is not
        limited1, _ = check_and_increment_rate_limit(db_path, t1["id"], max_messages=30)
        limited2, _ = check_and_increment_rate_limit(db_path, t2["id"], max_messages=30)
        assert limited1 is True
        assert limited2 is False
