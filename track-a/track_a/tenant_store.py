"""Multi-tenant persistence for Track A.

New tables added per PRODUCTION_SPEC_DETAILED.md §1:
- tenants: registry of onboarded business owners (sender_id → tenant)
- conversation_sessions: per-tenant conversation state (replaces in-memory SessionStore)
- processed_messages: webhook idempotency (checked BEFORE any AI call)
- rate_limit_buckets: fixed-window per-tenant rate limiting

All tables are additive to the existing inbound_messages / escalation_requests
schema in store.py.  The existing tables continue to work; new code paths
use these tables instead of (or alongside) the old ones.

Design notes:
- SQLite for pilot scale (single-server, single-worker).  Swap to Postgres
  for multi-worker if needed later — the schema is standard SQL.
- UUIDs generated in Python (uuid4) since SQLite has no native UUID type.
- WAL mode on every connection for concurrent read/write under FastAPI.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
-- §1.1: tenant registry
CREATE TABLE IF NOT EXISTS tenants (
    id                  TEXT PRIMARY KEY,          -- UUID
    sender_id           TEXT UNIQUE NOT NULL,       -- WhatsApp number / Telegram chat id
    business_name       TEXT NOT NULL DEFAULT '',
    wp_site_url         TEXT NOT NULL DEFAULT '',
    wp_app_username     TEXT NOT NULL DEFAULT '',
    wp_app_password_enc TEXT NOT NULL DEFAULT '',   -- encrypted at rest (Fernet)
    plan                TEXT NOT NULL DEFAULT 'founding',
    status              TEXT NOT NULL DEFAULT 'active',  -- active | degraded | suspended | onboarding
    onboarded_at        TEXT,                       -- ISO timestamp
    created_at          TEXT NOT NULL               -- ISO timestamp
);

-- §1.2: conversation sessions (one row per tenant)
CREATE TABLE IF NOT EXISTS conversation_sessions (
    id                  TEXT PRIMARY KEY,           -- UUID
    tenant_id           TEXT NOT NULL REFERENCES tenants(id),
    state               TEXT NOT NULL DEFAULT 'IDLE',
    pending_intent      TEXT,                       -- JSON (serialized intent object)
    context_history     TEXT NOT NULL DEFAULT '[]', -- JSON array of turns
    last_message_at     TEXT NOT NULL,              -- ISO timestamp
    expires_at          TEXT NOT NULL               -- ISO timestamp
);
CREATE INDEX IF NOT EXISTS idx_conv_sessions_tenant ON conversation_sessions(tenant_id);

-- §1.4: webhook idempotency (checked BEFORE any AI call)
CREATE TABLE IF NOT EXISTS processed_messages (
    id                  TEXT PRIMARY KEY,           -- UUID
    tenant_id           TEXT NOT NULL REFERENCES tenants(id),
    provider_message_id TEXT NOT NULL,
    processed_at        TEXT NOT NULL               -- ISO timestamp
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_processed_msg_unique
    ON processed_messages(tenant_id, provider_message_id);

-- §1.5: fixed-window rate limiting per tenant
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    tenant_id           TEXT PRIMARY KEY REFERENCES tenants(id),
    window_start        TEXT NOT NULL,              -- ISO timestamp
    message_count       INTEGER NOT NULL DEFAULT 0
);
"""


# ---------------------------------------------------------------------------
# Connection helper (same pattern as store.py)
# ---------------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _connection(db_path: Path):
    """Context manager that opens a connection, commits on success, and closes."""
    conn = _connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

def init_tenant_db(db_path: Path) -> None:
    """Create the new tables (idempotent)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connection(db_path) as conn:
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# Tenants (§1.1)
# ---------------------------------------------------------------------------

def create_tenant(
    db_path: Path,
    *,
    sender_id: str,
    business_name: str = "",
    wp_site_url: str = "",
    wp_app_username: str = "",
    wp_app_password_enc: str = "",
    plan: str = "founding",
    status: str = "onboarding",
) -> dict[str, Any]:
    """Insert a new tenant. Returns the created row as a dict.

    §7.2: wp_app_password_enc is encrypted at rest via encrypt_secret().
    Pass the PLAINTEXT password — this function encrypts before storing.
    """
    from .secrets import encrypt_secret

    tenant_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    encrypted_pw = encrypt_secret(wp_app_password_enc)
    with _connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tenants
                (id, sender_id, business_name, wp_site_url, wp_app_username,
                 wp_app_password_enc, plan, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tenant_id, sender_id, business_name, wp_site_url,
             wp_app_username, encrypted_pw, plan, status, now),
        )
    return get_tenant(db_path, tenant_id)


def get_tenant(db_path: Path, tenant_id: str) -> dict[str, Any] | None:
    """Fetch a tenant by UUID."""
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
    return dict(row) if row else None


def get_tenant_by_sender(db_path: Path, sender_id: str) -> dict[str, Any] | None:
    """Resolve tenant by WhatsApp number / Telegram chat id."""
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE sender_id = ?", (sender_id,)
        ).fetchone()
    return dict(row) if row else None


def update_tenant_status(db_path: Path, tenant_id: str, status: str) -> bool:
    """Update tenant status (active/degraded/suspended/onboarding)."""
    with _connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE tenants SET status = ? WHERE id = ?",
            (status, tenant_id),
        )
    return cur.rowcount > 0


def set_tenant_onboarded(db_path: Path, tenant_id: str) -> bool:
    """Mark tenant as onboarded (status=active, onboarded_at=now)."""
    now = datetime.now(UTC).isoformat()
    with _connection(db_path) as conn:
        cur = conn.execute(
            "UPDATE tenants SET status = 'active', onboarded_at = ? WHERE id = ?",
            (now, tenant_id),
        )
    return cur.rowcount > 0


def update_tenant_credentials(
    db_path: Path,
    tenant_id: str,
    *,
    wp_site_url: str,
    wp_app_username: str,
    wp_app_password_enc: str,
    business_name: str = "",
) -> bool:
    """Update WordPress credentials for a tenant.

    §7.2: wp_app_password_enc is encrypted at rest via encrypt_secret().
    Pass the PLAINTEXT password — this function encrypts before storing.
    """
    from .secrets import encrypt_secret

    encrypted_pw = encrypt_secret(wp_app_password_enc)
    with _connection(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE tenants
            SET wp_site_url = ?, wp_app_username = ?, wp_app_password_enc = ?,
                business_name = CASE WHEN ? = '' THEN business_name ELSE ? END
            WHERE id = ?
            """,
            (wp_site_url, wp_app_username, encrypted_pw,
             business_name, business_name, tenant_id),
        )
    return cur.rowcount > 0


def decrypt_tenant_password(db_path: Path, tenant_id: str) -> str:
    """§7.2: Decrypt a tenant's WordPress Application Password.

    Called only at the point of making the WP API call — never logged.
    """
    from .secrets import decrypt_secret

    tenant = get_tenant(db_path, tenant_id)
    if tenant is None:
        return ""
    return decrypt_secret(tenant.get("wp_app_password_enc", ""))


# ---------------------------------------------------------------------------
# Conversation sessions (§1.2)
# ---------------------------------------------------------------------------

def get_session(db_path: Path, tenant_id: str) -> dict[str, Any] | None:
    """Get the active conversation session for a tenant (or None if expired/missing)."""
    now = datetime.now(UTC).isoformat()
    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM conversation_sessions WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    if row is None:
        return None
    session = dict(row)
    # Check expiry
    if session["expires_at"] <= now:
        clear_session(db_path, tenant_id)
        return None
    return session


def upsert_session(
    db_path: Path,
    tenant_id: str,
    *,
    state: str,
    pending_intent: str | None = None,
    context_history: str = "[]",
    ttl_minutes: int = 15,
) -> dict[str, Any]:
    """Create or update a conversation session with a fresh expiry."""
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=ttl_minutes)
    now_iso = now.isoformat()
    expires_iso = expires.isoformat()
    session_id = str(uuid.uuid4())

    with _connection(db_path) as conn:
        # Check if session already exists
        existing = conn.execute(
            "SELECT id FROM conversation_sessions WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()

        if existing is not None:
            conn.execute(
                """
                UPDATE conversation_sessions
                SET state = ?, pending_intent = ?, context_history = ?,
                    last_message_at = ?, expires_at = ?
                WHERE tenant_id = ?
                """,
                (state, pending_intent, context_history, now_iso, expires_iso, tenant_id),
            )
            session_id = existing["id"]
        else:
            conn.execute(
                """
                INSERT INTO conversation_sessions
                    (id, tenant_id, state, pending_intent, context_history,
                     last_message_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, tenant_id, state, pending_intent, context_history,
                 now_iso, expires_iso),
            )

    return get_session(db_path, tenant_id)


def clear_session(db_path: Path, tenant_id: str) -> None:
    """Remove a tenant's conversation session."""
    with _connection(db_path) as conn:
        conn.execute(
            "DELETE FROM conversation_sessions WHERE tenant_id = ?",
            (tenant_id,),
        )


def cleanup_expired_sessions(db_path: Path) -> int:
    """Eagerly evict all expired sessions. Returns count removed."""
    now = datetime.now(UTC).isoformat()
    with _connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM conversation_sessions WHERE expires_at <= ?",
            (now,),
        )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Processed messages — idempotency (§1.4)
# ---------------------------------------------------------------------------

def mark_message_processed(
    db_path: Path,
    tenant_id: str,
    provider_message_id: str,
) -> bool:
    """Attempt to mark a message as processed.

    Returns True if this is a NEW message (insert succeeded).
    Returns False if it was already processed (duplicate — skip everything).
    This must be called BEFORE any AI call to avoid burning quota on duplicates.
    """
    msg_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    try:
        with _connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO processed_messages (id, tenant_id, provider_message_id, processed_at)
                VALUES (?, ?, ?, ?)
                """,
                (msg_id, tenant_id, provider_message_id, now),
            )
        return True
    except sqlite3.IntegrityError:
        return False  # duplicate — already processed


def purge_old_processed_messages(db_path: Path, days: int = 7) -> int:
    """Remove processed_messages older than `days` (provider retries don't last that long)."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with _connection(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM processed_messages WHERE processed_at < ?",
            (cutoff,),
        )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Rate limit buckets (§1.5)
# ---------------------------------------------------------------------------

def check_and_increment_rate_limit(
    db_path: Path,
    tenant_id: str,
    max_messages: int = 30,
    window_hours: int = 1,
) -> tuple[bool, int]:
    """Fixed-window rate limiter per tenant.

    Returns (is_limited, current_count).
    If is_limited is True, the caller should drop the message and reply
    with the rate-limit notice.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(hours=window_hours)
    now_iso = now.isoformat()
    window_start_iso = window_start.isoformat()

    with _connection(db_path) as conn:
        row = conn.execute(
            "SELECT window_start, message_count FROM rate_limit_buckets WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()

        if row is None:
            # First message from this tenant — create the bucket
            conn.execute(
                """
                INSERT INTO rate_limit_buckets (tenant_id, window_start, message_count)
                VALUES (?, ?, 1)
                """,
                (tenant_id, now_iso),
            )
            return False, 1

        bucket = dict(row)
        # Check if the window has expired
        if bucket["window_start"] <= window_start_iso:
            # Window expired — reset
            conn.execute(
                """
                UPDATE rate_limit_buckets
                SET window_start = ?, message_count = 1
                WHERE tenant_id = ?
                """,
                (now_iso, tenant_id),
            )
            return False, 1

        # Within the current window — increment
        new_count = bucket["message_count"] + 1
        if new_count > max_messages:
            return True, new_count

        conn.execute(
            "UPDATE rate_limit_buckets SET message_count = ? WHERE tenant_id = ?",
            (new_count, tenant_id),
        )
        return False, new_count
