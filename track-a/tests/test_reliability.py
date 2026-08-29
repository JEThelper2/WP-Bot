"""Tests for the reliability layer (§6).

Covers:
- §6.1: Idempotency — duplicate webhook deliveries are detected via processed_messages
- §6.2: Rate limiting — DB-backed fixed-window counter per tenant
- §6.3: Circuit breaker — retry + backoff + tenant status management
- §17: Error code mapping — WP REST errors → internal error codes
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from track_a.reliability import (
    CircuitBreaker,
    CircuitBreakerError,
    DBRateLimiter,
    IdempotencyChecker,
    ReliabilityLayer,
    classify_wp_error,
    owner_message_for_error,
)
from track_a.tenant_store import (
    create_tenant,
    get_tenant,
    init_tenant_db,
    update_tenant_status,
)

OWNER = "15551234567"
TENANT_ID: str | None = None


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test.db"
    init_tenant_db(db_path)
    global TENANT_ID
    tenant = create_tenant(db_path, sender_id=OWNER)
    TENANT_ID = tenant["id"]
    return db_path


# ---------------------------------------------------------------------------
# §6.1: Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_new_message_returns_true(self, db: Path) -> None:
        checker = IdempotencyChecker(db)
        assert checker.is_new(TENANT_ID, "msg-001") is True

    def test_duplicate_message_returns_false(self, db: Path) -> None:
        checker = IdempotencyChecker(db)
        assert checker.is_new(TENANT_ID, "msg-001") is True
        assert checker.is_new(TENANT_ID, "msg-001") is False

    def test_different_messages_both_new(self, db: Path) -> None:
        checker = IdempotencyChecker(db)
        assert checker.is_new(TENANT_ID, "msg-001") is True
        assert checker.is_new(TENANT_ID, "msg-002") is True

    def test_different_tenants_same_message_independent(self, db: Path) -> None:
        tenant2 = create_tenant(db_path=db, sender_id="15559999999")
        checker = IdempotencyChecker(db)
        assert checker.is_new(TENANT_ID, "msg-001") is True
        assert checker.is_new(tenant2["id"], "msg-001") is True


# ---------------------------------------------------------------------------
# §6.2: Rate limiting (DB-backed)
# ---------------------------------------------------------------------------


class TestDBRateLimiter:
    def test_first_message_not_limited(self, db: Path) -> None:
        limiter = DBRateLimiter(db, max_messages=30)
        is_limited, count = limiter.check(TENANT_ID)
        assert is_limited is False
        assert count == 1

    def test_under_limit_not_limited(self, db: Path) -> None:
        limiter = DBRateLimiter(db, max_messages=5)
        for _ in range(4):
            is_limited, _ = limiter.check(TENANT_ID)
            assert is_limited is False
        is_limited, count = limiter.check(TENANT_ID)
        assert is_limited is False
        assert count == 5

    def test_at_limit_limited(self, db: Path) -> None:
        limiter = DBRateLimiter(db, max_messages=3)
        for _ in range(3):
            limiter.check(TENANT_ID)
        is_limited, count = limiter.check(TENANT_ID)
        assert is_limited is True
        assert count == 4

    def test_window_reset(self, db: Path) -> None:
        limiter = DBRateLimiter(db, max_messages=2, window_hours=1)
        limiter.check(TENANT_ID)
        limiter.check(TENANT_ID)
        is_limited, _ = limiter.check(TENANT_ID)
        assert is_limited is True

        # Simulate window expiry by manipulating the window_start directly
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE rate_limit_buckets SET window_start = datetime('now', '-2 hours') WHERE tenant_id = ?",
                (TENANT_ID,),
            )
        is_limited, count = limiter.check(TENANT_ID)
        assert is_limited is False
        assert count == 1

    def test_independent_tenants(self, db: Path) -> None:
        tenant2 = create_tenant(db_path=db, sender_id="15559999999")
        limiter = DBRateLimiter(db, max_messages=2)
        limiter.check(TENANT_ID)
        limiter.check(TENANT_ID)
        # Tenant 1 is at limit
        assert limiter.check(TENANT_ID)[0] is True
        # Tenant 2 is not
        assert limiter.check(tenant2["id"])[0] is False


# ---------------------------------------------------------------------------
# §6.3: Circuit breaker
# ---------------------------------------------------------------------------


class FakeTrackBSuccess:
    """Always succeeds."""

    async def submit_intent(self, intent: dict, *, decision: str | None = None) -> dict:
        return {"status": "success"}


class FakeTrackBFail:
    """Always fails with a connection error (WP_UNREACHABLE)."""

    async def submit_intent(self, intent: dict, *, decision: str | None = None) -> dict:
        raise httpx.ConnectError("Connection refused")


class FakeTrackBAuthFail:
    """Always fails with 401 (AUTH_FAILED)."""

    async def submit_intent(self, intent: dict, *, decision: str | None = None) -> dict:
        response = httpx.Response(401, request=httpx.Request("POST", "http://fake"))
        raise httpx.HTTPStatusError("Unauthorized", request=response.request, response=response)


class FakeTrackBFailThenSucceed:
    """Fails twice, then succeeds."""

    def __init__(self) -> None:
        self.attempts = 0

    async def submit_intent(self, intent: dict, *, decision: str | None = None) -> dict:
        self.attempts += 1
        if self.attempts <= 2:
            raise httpx.ConnectError("Connection refused")
        return {"status": "success"}


class TestCircuitBreaker:
    def test_success_does_not_trip_breaker(self, db: Path) -> None:
        # Start tenant as active (circuit breaker only toggles active↔degraded)
        update_tenant_status(db, TENANT_ID, "active")
        breaker = CircuitBreaker(db, TENANT_ID, max_attempts=1, base_delay=0.01)
        client = FakeTrackBSuccess()

        result = asyncio.run(breaker.call(lambda: client.submit_intent({})))
        assert result["status"] == "success"
        tenant = get_tenant(db, TENANT_ID)
        assert tenant["status"] == "active"

    def test_failure_trips_breaker(self, db: Path) -> None:
        breaker = CircuitBreaker(db, TENANT_ID, max_attempts=1, base_delay=0.01)
        client = FakeTrackBFail()

        with pytest.raises(CircuitBreakerError) as exc_info:
            asyncio.run(breaker.call(lambda: client.submit_intent({})))
        assert exc_info.value.error_code == "WP_UNREACHABLE"
        tenant = get_tenant(db, TENANT_ID)
        assert tenant["status"] == "degraded"

    def test_auto_recover_on_success(self, db: Path) -> None:
        # Trip the breaker first
        update_tenant_status(db, TENANT_ID, "degraded")
        breaker = CircuitBreaker(db, TENANT_ID, max_attempts=1, base_delay=0.01)
        client = FakeTrackBSuccess()

        result = asyncio.run(breaker.call(lambda: client.submit_intent({})))
        assert result["status"] == "success"
        tenant = get_tenant(db, TENANT_ID)
        assert tenant["status"] == "active"

    def test_retry_then_success(self, db: Path) -> None:
        update_tenant_status(db, TENANT_ID, "active")
        client = FakeTrackBFailThenSucceed()
        breaker = CircuitBreaker(db, TENANT_ID, max_attempts=3, base_delay=0.01, max_delay=0.05)

        result = asyncio.run(breaker.call(lambda: client.submit_intent({})))
        assert result["status"] == "success"
        assert client.attempts == 3
        tenant = get_tenant(db, TENANT_ID)
        assert tenant["status"] == "active"

    def test_auth_failure_classifies_correctly(self, db: Path) -> None:
        breaker = CircuitBreaker(db, TENANT_ID, max_attempts=1, base_delay=0.01)
        client = FakeTrackBAuthFail()

        with pytest.raises(CircuitBreakerError) as exc_info:
            asyncio.run(breaker.call(lambda: client.submit_intent({})))
        assert exc_info.value.error_code == "AUTH_FAILED"
        assert "contact support" in exc_info.value.owner_message

    def test_alert_fn_called_on_transition(self, db: Path) -> None:
        update_tenant_status(db, TENANT_ID, "active")
        alert_fn = MagicMock()
        breaker = CircuitBreaker(db, TENANT_ID, max_attempts=1, base_delay=0.01, alert_fn=alert_fn)
        client = FakeTrackBFail()

        with pytest.raises(CircuitBreakerError):
            asyncio.run(breaker.call(lambda: client.submit_intent({})))
        alert_fn.assert_called_once()
        assert "degraded" in alert_fn.call_args[0][0]

    def test_alert_fn_not_called_on_already_degraded(self, db: Path) -> None:
        update_tenant_status(db, TENANT_ID, "degraded")
        alert_fn = MagicMock()
        breaker = CircuitBreaker(db, TENANT_ID, max_attempts=1, base_delay=0.01, alert_fn=alert_fn)
        client = FakeTrackBFail()

        with pytest.raises(CircuitBreakerError):
            asyncio.run(breaker.call(lambda: client.submit_intent({})))
        alert_fn.assert_not_called()


# ---------------------------------------------------------------------------
# §17: Error code mapping
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_connection_error_is_wp_unreachable(self) -> None:
        assert classify_wp_error(httpx.ConnectError("refused")) == "WP_UNREACHABLE"

    def test_timeout_is_wp_unreachable(self) -> None:
        assert classify_wp_error(httpx.TimeoutException("timed out")) == "WP_UNREACHABLE"

    def test_dns_error_is_wp_unreachable(self) -> None:
        assert classify_wp_error(OSError("Name or service not known")) == "WP_UNREACHABLE"

    def test_401_is_auth_failed(self) -> None:
        response = httpx.Response(401, request=httpx.Request("POST", "http://fake"))
        exc = httpx.HTTPStatusError("Unauthorized", request=response.request, response=response)
        assert classify_wp_error(exc) == "AUTH_FAILED"

    def test_403_is_auth_failed(self) -> None:
        response = httpx.Response(403, request=httpx.Request("POST", "http://fake"))
        exc = httpx.HTTPStatusError("Forbidden", request=response.request, response=response)
        assert classify_wp_error(exc) == "AUTH_FAILED"

    def test_404_is_entity_not_found(self) -> None:
        response = httpx.Response(404, request=httpx.Request("POST", "http://fake"))
        exc = httpx.HTTPStatusError("Not Found", request=response.request, response=response)
        assert classify_wp_error(exc) == "ENTITY_NOT_FOUND"

    def test_500_with_fatal_error_is_plugin_conflict(self) -> None:
        response = httpx.Response(
            500,
            request=httpx.Request("POST", "http://fake"),
            text="Fatal error: Allowed memory size exhausted",
        )
        exc = httpx.HTTPStatusError("Server Error", request=response.request, response=response)
        assert classify_wp_error(exc) == "PLUGIN_CONFLICT"

    def test_500_without_fatal_is_unknown(self) -> None:
        response = httpx.Response(
            500,
            request=httpx.Request("POST", "http://fake"),
            text="Internal Server Error",
        )
        exc = httpx.HTTPStatusError("Server Error", request=response.request, response=response)
        assert classify_wp_error(exc) == "UNKNOWN"

    def test_generic_exception_is_unknown(self) -> None:
        assert classify_wp_error(ValueError("something")) == "UNKNOWN"


class TestOwnerMessages:
    def test_wp_unreachable_message(self) -> None:
        msg = owner_message_for_error("WP_UNREACHABLE")
        assert "couldn't reach" in msg.lower()
        assert "contact support" not in msg.lower()  # different from PLUGIN_CONFLICT

    def test_auth_failed_message(self) -> None:
        msg = owner_message_for_error("AUTH_FAILED")
        assert "lost access" in msg.lower()
        assert "contact support" in msg.lower()

    def test_plugin_conflict_message(self) -> None:
        msg = owner_message_for_error("PLUGIN_CONFLICT")
        assert "contact support" in msg.lower()

    def test_unknown_message(self) -> None:
        msg = owner_message_for_error("UNKNOWN")
        assert "contact support" in msg.lower()


# ---------------------------------------------------------------------------
# Combined ReliabilityLayer
# ---------------------------------------------------------------------------


class TestReliabilityLayer:
    def test_idempotency_through_layer(self, db: Path) -> None:
        layer = ReliabilityLayer(db)
        assert layer.check_idempotency(TENANT_ID, "msg-001") is True
        assert layer.check_idempotency(TENANT_ID, "msg-001") is False

    def test_rate_limit_through_layer(self, db: Path) -> None:
        layer = ReliabilityLayer(db, max_messages=2)
        assert layer.check_rate_limit(TENANT_ID) == (False, 1)
        assert layer.check_rate_limit(TENANT_ID) == (False, 2)
        assert layer.check_rate_limit(TENANT_ID)[0] is True

    def test_circuit_breaker_through_layer(self, db: Path) -> None:
        layer = ReliabilityLayer(db)
        breaker = layer.circuit_breaker(TENANT_ID)
        assert isinstance(breaker, CircuitBreaker)
