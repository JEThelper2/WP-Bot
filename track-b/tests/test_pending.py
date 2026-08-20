"""Pending-confirmation store: stage -> YES releases the intent for the
write pipeline; NO discards; nothing staged is a clear no-op; a YES after
TTL expiry is reported as expired and never executes a stale write."""

import asyncio

import pytest

from shared_contract import CONTRACT_VERSION, ContractValidationError
from track_b.pending import (
    PENDING_TTL_SECONDS,
    InMemoryPendingStore,
    RedisPendingStore,
)

OWNER = "15551234567"


def run(coro):
    return asyncio.run(coro)


def make_intent(**overrides):
    intent = {
        "contract_version": CONTRACT_VERSION,
        "owner_id": OWNER,
        "action": "create",
        "content_type": "job",
        "fields": {"title": "Barista", "description": "$18/hr"},
        "confidence": 0.95,
    }
    intent.update(overrides)
    return intent


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def store(clock: Clock) -> InMemoryPendingStore:
    return InMemoryPendingStore(time_fn=clock.time)


# ------------------------------------------------------- the required flows


def test_stage_then_yes_returns_the_staged_intent(store):
    intent = make_intent()
    change_id = run(store.stage_pending(intent))
    assert change_id.startswith("pc-")

    outcome = run(store.resolve_pending(OWNER, "yes"))
    assert outcome.kind == "released"
    assert outcome.change_id == change_id
    assert outcome.intent == intent  # the exact staged object
    # A released confirmation is gone — a second resolve finds nothing.
    assert run(store.resolve_pending(OWNER, "yes")).kind == "nothing_pending"


def test_stage_then_no_discards(store):
    change_id = run(store.stage_pending(make_intent()))

    outcome = run(store.resolve_pending(OWNER, "no"))
    assert outcome.kind == "discarded"
    assert outcome.change_id == change_id
    assert outcome.intent is None
    # Discarded state is cleared — nothing lingers, nothing to write.
    assert run(store.resolve_pending(OWNER, "no")).kind == "nothing_pending"


def test_resolve_with_nothing_staged_is_clear_noop(store):
    outcome = run(store.resolve_pending(OWNER, "yes"))
    assert outcome.kind == "nothing_pending"
    assert "nothing is pending" in outcome.message

    outcome = run(store.resolve_pending(OWNER, "no"))
    assert outcome.kind == "nothing_pending"


def test_yes_after_ttl_expiry_is_expired_not_released(store, clock):
    run(store.stage_pending(make_intent()))
    clock.advance(PENDING_TTL_SECONDS + 1)  # window closed

    outcome = run(store.resolve_pending(OWNER, "yes"))
    assert outcome.kind == "expired"
    assert outcome.intent is None  # the write is NOT released
    assert "window has passed" in outcome.message
    # Expired state is cleaned up; next resolve is a plain no-op.
    assert run(store.resolve_pending(OWNER, "yes")).kind == "nothing_pending"


def test_no_after_ttl_expiry_is_also_expired(store, clock):
    run(store.stage_pending(make_intent()))
    clock.advance(PENDING_TTL_SECONDS + 1)
    outcome = run(store.resolve_pending(OWNER, "no"))
    assert outcome.kind == "expired"


# ------------------------------------------------------- robustness


def test_decision_accepts_booleans_and_case_variants(store):
    change_id = run(store.stage_pending(make_intent()))
    assert run(store.resolve_pending(OWNER, True)).kind == "released"
    assert change_id

    run(store.stage_pending(make_intent()))
    assert run(store.resolve_pending(OWNER, "NO")).kind == "discarded"

    run(store.stage_pending(make_intent()))
    assert run(store.resolve_pending(OWNER, "y")).kind == "released"


def test_invalid_decision_raises(store):
    run(store.stage_pending(make_intent()))
    with pytest.raises(ValueError, match="decision must be 'yes' or 'no'"):
        run(store.resolve_pending(OWNER, "maybe"))
    # The pending state is untouched by a bad call.
    assert run(store.resolve_pending(OWNER, "yes")).kind == "released"


def test_stage_validates_the_intent(store):
    malformed = make_intent()
    del malformed["confidence"]
    with pytest.raises(ContractValidationError):
        run(store.stage_pending(malformed))
    # Nothing was staged.
    assert run(store.resolve_pending(OWNER, "yes")).kind == "nothing_pending"


def test_restage_overwrites_the_previous_pending(store):
    first = run(store.stage_pending(make_intent(fields={"title": "Old", "description": "x"})))
    second = run(store.stage_pending(make_intent(fields={"title": "New", "description": "x"})))

    assert first != second
    outcome = run(store.resolve_pending(OWNER, "yes"))
    assert outcome.change_id == second
    assert outcome.intent["fields"]["title"] == "New"


# ------------------------------------------------------- Redis implementation


@pytest.fixture()
def redis_store() -> RedisPendingStore:
    import fakeredis.aioredis

    return RedisPendingStore(fakeredis.aioredis.FakeRedis())


def test_redis_stage_then_yes(redis_store):
    intent = make_intent()
    change_id = run(redis_store.stage_pending(intent))
    outcome = run(redis_store.resolve_pending(OWNER, "yes"))
    assert outcome.kind == "released"
    assert outcome.change_id == change_id
    assert outcome.intent == intent


def test_redis_stage_then_no(redis_store):
    run(redis_store.stage_pending(make_intent()))
    assert run(redis_store.resolve_pending(OWNER, "no")).kind == "discarded"
    assert run(redis_store.resolve_pending(OWNER, "yes")).kind == "nothing_pending"


def test_redis_nothing_staged(redis_store):
    assert run(redis_store.resolve_pending(OWNER, "yes")).kind == "nothing_pending"


def test_redis_yes_after_ttl_expiry_is_expired(redis_store):
    # A 1-second real TTL, then wait past it (fakeredis uses the real clock
    # and expires keys >= 1s as Redis itself would).
    import time as _time

    redis_store.ttl = 1
    run(redis_store.stage_pending(make_intent()))
    _time.sleep(1.2)

    outcome = run(redis_store.resolve_pending(OWNER, "yes"))
    assert outcome.kind == "expired"
    assert outcome.intent is None
    assert "window has passed" in outcome.message


def test_resolve_outcomes_are_single_use(redis_store):
    run(redis_store.stage_pending(make_intent()))
    assert run(redis_store.resolve_pending(OWNER, "yes")).kind == "released"
    # The released record cannot be resolved twice (no double-write).
    assert run(redis_store.resolve_pending(OWNER, "yes")).kind == "nothing_pending"
