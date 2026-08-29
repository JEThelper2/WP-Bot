"""Short-lived per-owner conversation state machine (§3).

Four states per PRODUCTION_SPEC_DETAILED.md §3.1:

- ``IDLE``                    — no open conversation, next message parsed fresh.
- ``AWAITING_CLARIFICATION``  — bot asked a question, waiting on a specific answer.
- ``AWAITING_CONFIRMATION``   — bot asked yes/no before a destructive/high-impact action.
- ``EXECUTING``               — action in flight (transient; prevents double-submit).

Transitions (§3.1):
- IDLE → AWAITING_CLARIFICATION: confidence < 0.7, required slot missing, or ambiguous entity.
- IDLE → AWAITING_CONFIRMATION: destructive action (delete, business_info, page content update).
- IDLE → EXECUTING: unambiguous, non-destructive action (e.g. menu_item_add).
- AWAITING_CLARIFICATION → IDLE: owner resolves the missing/ambiguous slot.
- AWAITING_CONFIRMATION → EXECUTING: owner replies affirmative.
- AWAITING_CONFIRMATION → IDLE: owner replies negative or unrelated (cancel).
- EXECUTING → IDLE: action completes (success or failure).
- Any state → IDLE: session expiry (15 min inactivity).

This is deliberately *not* the source of truth for any content — Track B
owns site state and the change log. This is just enough in-memory state
to hold a conversation together inside Track A. In-memory is fine for a
single worker; a multi-worker deployment should swap ``SessionStore`` for a
Redis-backed implementation with the same interface.

Sessions expire after ``SESSION_TTL_SECONDS`` (default 15 minutes, matching
Track B's pending confirmation window).  Stale sessions are lazily cleaned
up on ``get()`` and eagerly swept by ``cleanup()``.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# How long a session may sit idle before it is considered stale.
SESSION_TTL_SECONDS = 15 * 60


@dataclass
class SessionState:
    state: str = "IDLE"  # IDLE | AWAITING_CLARIFICATION | AWAITING_CONFIRMATION | EXECUTING | VOICE_AWAITING_ECHO
    pending_intent: dict[str, Any] | None = None  # last parsed intent
    asked_field: str | None = None  # field the last clarification question targeted
    turns: int = 0  # clarification turns consumed so far
    re_ask_count: int = 0  # §3.3: confirmation re-ask counter (max 1 re-ask)
    # §3.2: context_history — last 6 turns (3 owner + 3 bot), used for LLM re-entry.
    context_history: list[dict[str, str]] = field(default_factory=list)
    # Legacy alias for context_history (used by existing exchange-based code).
    exchange: list[dict[str, str]] = field(default_factory=list)
    original_message: str | None = None  # retained for escalation logging
    site_id: str | None = None  # active site for multi-site owners
    expires_at: float = 0.0  # monotonic expiry timestamp (set by SessionStore)
    # §4.1: voice echo confirmation — transcript waiting for owner verification.
    voice_transcript: str | None = None  # the transcribed text awaiting echo-confirm
    voice_confidence: float = 0.0  # confidence score for the voice note
    source: str = "text"  # 'text' | 'voice' — tracks how the current request originated


class SessionStore:
    """Thread-safe in-memory session store keyed by owner_id.

    Sessions expire after ``ttl`` seconds of inactivity.  Expired entries
    are lazily evicted on ``get()`` and swept in bulk by ``cleanup()``.
    """

    def __init__(self, ttl: int = SESSION_TTL_SECONDS, time_fn: Any = None) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()
        self._ttl = ttl
        self._time = time_fn or time.monotonic

    def get(self, owner_id: str) -> SessionState | None:
        now = self._time()
        with self._lock:
            state = self._sessions.get(owner_id)
            if state is not None and state.expires_at <= now:
                # Stale: evict and pretend it was never there.
                del self._sessions[owner_id]
                return None
            return state

    def set(self, owner_id: str, state: SessionState) -> None:
        state.expires_at = self._time() + self._ttl
        with self._lock:
            self._sessions[owner_id] = state

    def clear(self, owner_id: str) -> None:
        with self._lock:
            self._sessions.pop(owner_id, None)

    def cleanup(self) -> int:
        """Eagerly evict all expired sessions.  Returns the count removed."""
        now = self._time()
        with self._lock:
            stale = [k for k, s in self._sessions.items() if s.expires_at <= now]
            for k in stale:
                del self._sessions[k]
            return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


class ActiveSiteStore:
    """Thread-safe in-memory store for each owner's last active site.

    This is independent of the session lifecycle: when a session is cleared
    (e.g. after a successful confirmation), the active site persists so
    that commands like UNDO can still target the correct site.
    """

    def __init__(self) -> None:
        self._sites: dict[str, str] = {}  # owner_id -> site_id
        self._lock = threading.Lock()

    def get(self, owner_id: str) -> str | None:
        with self._lock:
            return self._sites.get(owner_id)

    def set(self, owner_id: str, site_id: str) -> None:
        with self._lock:
            self._sites[owner_id] = site_id

    def clear(self, owner_id: str) -> None:
        with self._lock:
            self._sites.pop(owner_id, None)


class DBSessionStore:
    """DB-backed conversation session store (§1.2) — same interface as SessionStore.

    Keyed by owner_id (WhatsApp phone number) like SessionStore, but
    persists to the conversation_sessions table.  Resolves owner_id →
    tenant_id internally via get_tenant_by_sender.

    SessionState fields that don't have their own columns are packed
    into the pending_intent column as a wrapper JSON blob (underscore-
    prefixed keys to avoid collision with actual intent fields).
    """

    # Wrapper keys packed into pending_intent column
    _WRAPPER_KEYS = {
        "_asked_field", "_turns", "_re_ask_count", "_site_id",
        "_voice_transcript", "_voice_confidence", "_source",
        "_exchange", "_original_message",
    }

    def __init__(self, db_path: Path, *, ttl_minutes: int = 15) -> None:
        self._db_path = db_path
        self._ttl = ttl_minutes

    def get(self, owner_id: str) -> SessionState | None:
        from .tenant_store import get_session, get_tenant_by_sender

        tenant = get_tenant_by_sender(self._db_path, owner_id)
        if tenant is None:
            return None
        row = get_session(self._db_path, tenant["id"])
        if row is None:
            return None
        return self._row_to_state(row)

    def set(self, owner_id: str, state: SessionState) -> None:
        from .tenant_store import get_tenant_by_sender, upsert_session

        tenant = get_tenant_by_sender(self._db_path, owner_id)
        if tenant is None:
            # No tenant record yet — can't persist. Store nothing.
            return
        intent_blob = self._pack_intent(state)
        context_json = json.dumps(state.context_history or state.exchange)
        upsert_session(
            self._db_path,
            tenant["id"],
            state=state.state,
            pending_intent=intent_blob,
            context_history=context_json,
            ttl_minutes=self._ttl,
        )

    def clear(self, owner_id: str) -> None:
        from .tenant_store import clear_session, get_tenant_by_sender

        tenant = get_tenant_by_sender(self._db_path, owner_id)
        if tenant is not None:
            clear_session(self._db_path, tenant["id"])

    def cleanup(self) -> int:
        from .tenant_store import cleanup_expired_sessions

        return cleanup_expired_sessions(self._db_path)

    def __len__(self) -> int:
        # Count non-expired sessions — not performance-critical for pilot.
        import sqlite3

        now = datetime.now(UTC).isoformat()
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM conversation_sessions WHERE expires_at > ?",
                (now,),
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    # -- serialization helpers -----------------------------------------------

    def _pack_intent(self, state: SessionState) -> str | None:
        """Serialize SessionState into a JSON blob for the pending_intent column.

        The actual intent (if any) goes under a non-underscore key.
        Extra SessionState fields go under underscore-prefixed keys.
        """
        blob: dict[str, Any] = {}
        if state.pending_intent is not None:
            blob["_intent"] = state.pending_intent
        if state.asked_field is not None:
            blob["_asked_field"] = state.asked_field
        if state.turns:
            blob["_turns"] = state.turns
        if state.re_ask_count:
            blob["_re_ask_count"] = state.re_ask_count
        if state.site_id is not None:
            blob["_site_id"] = state.site_id
        if state.voice_transcript is not None:
            blob["_voice_transcript"] = state.voice_transcript
        if state.voice_confidence:
            blob["_voice_confidence"] = state.voice_confidence
        if state.source and state.source != "text":
            blob["_source"] = state.source
        if state.exchange:
            blob["_exchange"] = state.exchange
        if state.original_message is not None:
            blob["_original_message"] = state.original_message
        return json.dumps(blob) if blob else None

    def _unpack_intent(self, raw: str | None) -> dict[str, Any]:
        """Deserialize the pending_intent column blob back to a dict."""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    def _row_to_state(self, row: dict) -> SessionState:
        """Convert a conversation_sessions row to a SessionState."""
        blob = self._unpack_intent(row.get("pending_intent"))
        context = []
        raw_ctx = row.get("context_history", "[]")
        if raw_ctx:
            try:
                context = json.loads(raw_ctx)
            except (json.JSONDecodeError, TypeError):
                context = []

        # Parse expires_at from ISO timestamp to monotonic-ish value
        # so the rest of the code can compare using the same convention.
        expires_at = 0.0
        raw_expires = row.get("expires_at")
        if raw_expires:
            try:
                exp_dt = datetime.fromisoformat(raw_expires)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=UTC)
                # Store as epoch seconds — the in-memory SessionStore uses
                # monotonic, but for DB-backed sessions we use wall clock
                # since we re-check expiry via SQL on every get().
                expires_at = exp_dt.timestamp()
            except (ValueError, TypeError):
                expires_at = 0.0

        return SessionState(
            state=row.get("state", "IDLE"),
            pending_intent=blob.get("_intent"),
            asked_field=blob.get("_asked_field"),
            turns=blob.get("_turns", 0),
            re_ask_count=blob.get("_re_ask_count", 0),
            context_history=context,
            exchange=blob.get("_exchange", context),  # fall back to context_history
            site_id=blob.get("_site_id"),
            voice_transcript=blob.get("_voice_transcript"),
            voice_confidence=blob.get("_voice_confidence", 0.0),
            source=blob.get("_source", "text"),
            original_message=blob.get("_original_message"),
            expires_at=expires_at,
        )
