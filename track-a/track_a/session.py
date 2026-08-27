"""Short-lived per-owner conversation state for the clarification loop.

After A3 parsing, the router (A4) may need to hold on to a little state
per owner so a follow-up message can re-enter intent parsing with the
context of the prior exchange:

- `clarify`  — we asked a targeted question; `pending_intent` / the
  `exchange` transcript give the next parse call the prior context.
- `escalate` — we offered to connect the owner with a developer;
  `original_message` is what triggered it, used when they reply "yes".

This is deliberately *not* the source of truth for any content — Track B
owns site state and the change log. This is just enough in-memory state
to hold a conversation together inside Track A. In-memory is fine for a
single worker; a multi-worker deployment should swap `SessionStore` for a
Redis-backed implementation with the same two-method interface.

Sessions expire after ``SESSION_TTL_SECONDS`` (default 15 minutes, matching
Track B's pending confirmation window).  Stale sessions are lazily cleaned
up on ``get()`` and eagerly swept by ``cleanup()``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

# How long a session may sit idle before it is considered stale.
SESSION_TTL_SECONDS = 15 * 60


@dataclass
class SessionState:
    branch: str | None = None  # "clarify" | "escalate" | "confirm" | None
    pending_intent: dict[str, Any] | None = None  # last parsed intent
    asked_field: str | None = None  # field the last question targeted
    turns: int = 0  # clarification turns consumed so far
    # Transcript of the exchange (owner messages + our replies), newest
    # last, used to build the LLM context on re-entry.
    exchange: list[dict[str, str]] = field(default_factory=list)
    original_message: str | None = None  # message that triggered escalation
    site_id: str | None = None  # active site for multi-site owners
    expires_at: float = 0.0  # monotonic expiry timestamp (set by SessionStore)


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
