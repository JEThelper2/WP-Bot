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
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionState:
    branch: str | None = None  # "clarify" | "escalate" | None
    pending_intent: dict[str, Any] | None = None  # last parsed intent
    asked_field: str | None = None  # field the last question targeted
    turns: int = 0  # clarification turns consumed so far
    # Transcript of the exchange (owner messages + our replies), newest
    # last, used to build the LLM context on re-entry.
    exchange: list[dict[str, str]] = field(default_factory=list)
    original_message: str | None = None  # message that triggered escalation


class SessionStore:
    """Thread-safe in-memory session store keyed by owner_id."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def get(self, owner_id: str) -> SessionState | None:
        with self._lock:
            return self._sessions.get(owner_id)

    def set(self, owner_id: str, state: SessionState) -> None:
        with self._lock:
            self._sessions[owner_id] = state

    def clear(self, owner_id: str) -> None:
        with self._lock:
            self._sessions.pop(owner_id, None)
