"""Pending-confirmation state layer for Track B.

A Track A intent that has passed allowlist validation (B2) but is
awaiting the owner's YES/NO is **staged** here before any write:

- `stage_pending(intent)` stores the full intent object (keyed by the
  intent's `owner_id`) together with a generated pending `change_id`,
  with a TTL so a pending confirmation can't sit around forever
  (`PENDING_TTL_SECONDS`, 15 minutes by default).
- `resolve_pending(owner_id, decision)` is the only release valve:
  - `yes`  → returns the staged intent for the write pipeline to execute
    (B1 + B2); if the TTL already passed, returns an `expired` outcome
    instead — **a stale write is never executed**;
  - `no`   → discards the pending state and returns a discard outcome;
  - nothing staged → a clear `nothing_pending` outcome, never an
    ambiguous error.

Expiry is distinguishable from "never staged": a tiny meta key with a
slightly longer TTL (intent TTL + a grace window) survives the intent key
long enough to report a late YES as `expired`, not silently dropped.

`RedisPendingStore` is the production implementation (TTL is enforced by
Redis itself). `InMemoryPendingStore` exists for tests and single-worker
dev, with an injectable clock so expiry is deterministic.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from shared_contract import validate_intent

logger = logging.getLogger("track_b.pending")

# How long a staged confirmation may wait for the owner's decision.
PENDING_TTL_SECONDS = 15 * 60

# The meta key (used to detect expiry) outlives the intent key by this
# grace period, so a YES that lands shortly after expiry is reported as
# "window passed" rather than silently dropped.
PENDING_EXPIRY_GRACE_SECONDS = 60

_KEY_PREFIX = "wpbot:pending"


def _intent_key(owner_id: str) -> str:
    return f"{_KEY_PREFIX}:{owner_id}"


def _meta_key(owner_id: str) -> str:
    return f"{_KEY_PREFIX}:{owner_id}:meta"


def _normalize_decision(decision: Any) -> str:
    """Accept 'yes'/'no' (case-insensitive) or booleans; reject the rest."""
    if isinstance(decision, bool):
        return "yes" if decision else "no"
    value = str(decision).strip().lower()
    if value in ("yes", "y"):
        return "yes"
    if value in ("no", "n"):
        return "no"
    raise ValueError(f"decision must be 'yes' or 'no', got {decision!r}")


@dataclass(frozen=True)
class ResolveOutcome:
    """Result of resolving a pending confirmation.

    kind:
    - `released`       — decision was YES and the window was open; `intent`
                         is handed to the write pipeline.
    - `discarded`      — decision was NO; pending state cleared, nothing to
                         write.
    - `nothing_pending`— no staged intent existed for this owner.
    - `expired`        — the confirmation window (TTL) passed; the owner
                         must resend the request.
    """

    kind: str
    change_id: str | None = None
    intent: dict[str, Any] | None = None
    message: str = ""

    @classmethod
    def released(cls, change_id: str, intent: dict[str, Any]) -> ResolveOutcome:
        return cls(
            kind="released",
            change_id=change_id,
            intent=intent,
            message="confirmation accepted; intent released for execution",
        )

    @classmethod
    def discarded(cls, change_id: str) -> ResolveOutcome:
        return cls(
            kind="discarded",
            change_id=change_id,
            message="confirmation declined; pending intent discarded",
        )

    @classmethod
    def nothing_pending(cls) -> ResolveOutcome:
        return cls(
            kind="nothing_pending",
            message="nothing is pending for this owner",
        )

    @classmethod
    def expired(cls, change_id: str) -> ResolveOutcome:
        return cls(
            kind="expired",
            change_id=change_id,
            message="the confirmation window has passed — please resend the request",
        )


class PendingStore(Protocol):
    async def stage_pending(self, intent: dict[str, Any]) -> str: ...

    async def resolve_pending(self, owner_id: str, decision: Any) -> ResolveOutcome: ...


class RedisPendingStore:
    """Production store: TTL enforced by Redis itself.

    A second meta key with a slightly longer TTL (intent TTL + grace)
    survives the intent key, so a late YES is reported as `expired`
    ("window passed — resend") instead of silently becoming
    `nothing_pending`.
    """

    def __init__(
        self,
        redis: Any,
        ttl: int = PENDING_TTL_SECONDS,
        grace: int = PENDING_EXPIRY_GRACE_SECONDS,
    ) -> None:
        self._redis = redis
        self.ttl = ttl
        self.grace = grace

    async def stage_pending(self, intent: dict[str, Any]) -> str:
        validate_intent(intent)  # boundary discipline: never stage garbage
        owner_id = intent["owner_id"]
        change_id = f"pc-{uuid.uuid4().hex[:12]}"
        payload = json.dumps({"change_id": change_id, "intent": intent, "staged_at": time.time()})
        pipe = self._redis.pipeline()
        pipe.set(_intent_key(owner_id), payload, ex=self.ttl)
        pipe.set(_meta_key(owner_id), change_id, ex=self.ttl + self.grace)
        await pipe.execute()
        logger.info(
            "staged pending confirmation for owner %s (change_id=%s, ttl=%ss)",
            owner_id,
            change_id,
            self.ttl,
        )
        return change_id

    async def resolve_pending(self, owner_id: str, decision: Any) -> ResolveOutcome:
        decision = _normalize_decision(decision)
        payload = await self._redis.get(_intent_key(owner_id))
        if payload is not None:
            record = json.loads(payload)
            await self._redis.delete(_intent_key(owner_id), _meta_key(owner_id))
            if decision == "yes":
                return ResolveOutcome.released(record["change_id"], record["intent"])
            return ResolveOutcome.discarded(record["change_id"])

        # Intent key gone: either it expired or was never staged. The meta
        # key (same TTL) disambiguates.
        change_id = await self._redis.get(_meta_key(owner_id))
        if change_id is not None:
            await self._redis.delete(_meta_key(owner_id))
            logger.info("pending confirmation for owner %s expired", owner_id)
            return ResolveOutcome.expired(change_id)
        return ResolveOutcome.nothing_pending()


class InMemoryPendingStore:
    """Test/dev store with an injectable clock for deterministic expiry."""

    def __init__(
        self,
        ttl: int = PENDING_TTL_SECONDS,
        time_fn: Any = time.time,
    ) -> None:
        self.ttl = ttl
        self._time = time_fn
        self._store: dict[str, dict[str, Any]] = {}

    async def stage_pending(self, intent: dict[str, Any]) -> str:
        validate_intent(intent)
        owner_id = intent["owner_id"]
        change_id = f"pc-{uuid.uuid4().hex[:12]}"
        self._store[owner_id] = {
            "change_id": change_id,
            "intent": intent,
            "expires_at": self._time() + self.ttl,
        }
        return change_id

    async def resolve_pending(self, owner_id: str, decision: Any) -> ResolveOutcome:
        decision = _normalize_decision(decision)
        record = self._store.get(owner_id)
        if record is None:
            return ResolveOutcome.nothing_pending()

        if record["expires_at"] <= self._time():
            del self._store[owner_id]
            logger.info("pending confirmation for owner %s expired", owner_id)
            return ResolveOutcome.expired(record["change_id"])

        del self._store[owner_id]
        if decision == "yes":
            return ResolveOutcome.released(record["change_id"], record["intent"])
        return ResolveOutcome.discarded(record["change_id"])


def create_redis_pending_store(redis_url: str | None = None) -> RedisPendingStore:
    """Factory for the production store (used by the app wiring)."""
    import redis.asyncio as aioredis

    url = redis_url or "redis://localhost:6379/0"
    return RedisPendingStore(aioredis.from_url(url))
