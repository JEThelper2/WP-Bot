"""Reliability layer for the inbound message pipeline.

Per PRODUCTION_SPEC_DETAILED.md §6:

- §6.1  Idempotency:  processed_messages table checked BEFORE any AI call.
        Duplicate webhook deliveries return HTTP 200 and skip all processing.
- §6.2  Rate limiting: fixed-window counter per tenant (30 messages/hr default),
        backed by the rate_limit_buckets table (survives restarts).
- §6.3  Circuit breaker: WP REST API failures retry 3x with exponential backoff
        (1s, 3s, 9s).  On final failure, tenant.status is set to 'degraded'.
        Next successful WP call auto-recovers to 'active'.  A single operator
        alert fires on transition to degraded.

Design:
- All three mechanisms share a single `ReliabilityLayer` instance wired into
  the FastAPI app at startup.
- The circuit breaker wraps individual Track B calls, not the whole request —
  so a failed undo doesn't block a subsequent menu_item_add.
- Error code mapping (§17) translates raw WP REST errors into the contract's
  error.code enum, which drives the owner-facing messages.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .tenant_store import (
    check_and_increment_rate_limit,
    get_tenant,
    mark_message_processed,
    update_tenant_status,
)

logger = logging.getLogger("track_a.reliability")

# ---------------------------------------------------------------------------
# §6.3  Error code mapping (WP REST behavior → internal error.code)
# ---------------------------------------------------------------------------

# Owner-facing messages per §6.3.  Never raw error dumps.
_ERROR_OWNER_MESSAGES: dict[str, str] = {
    "WP_UNREACHABLE": (
        "I couldn't reach your website right now. I'll let you know once "
        "it's back — your team can also check the site is online."
    ),
    "AUTH_FAILED": (
        "I lost access to your website. This usually means a password or "
        "plugin setting changed — please contact support."
    ),
    "PLUGIN_CONFLICT": (
        "Something on your website is blocking this update. Please contact "
        "support so we can take a look."
    ),
    "ENTITY_NOT_FOUND": (
        "I couldn't find that item on your website. Could you check the "
        "name and try again?"
    ),
    "UNKNOWN": (
        "Something on your website is blocking this update. Please contact "
        "support so we can take a look."
    ),
}


def classify_wp_error(exc: Exception) -> str:
    """Map a raw exception from the WP REST client to an internal error.code.

    Follows the mapping in §17 of the appendix:
    - Connection timeout/refused/DNS → WP_UNREACHABLE
    - HTTP 401/403                 → AUTH_FAILED
    - HTTP 404 (known entity)      → ENTITY_NOT_FOUND
    - HTTP 500 + "Fatal error"     → PLUGIN_CONFLICT
    - Everything else              → UNKNOWN
    """
    import httpx

    if isinstance(exc, httpx.TimeoutException | httpx.ConnectError | OSError):
        return "WP_UNREACHABLE"

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return "AUTH_FAILED"
        if status == 404:
            return "ENTITY_NOT_FOUND"
        if status >= 500:
            body = ""
            try:
                body = exc.response.text[:500]
            except Exception:
                pass
            if "Fatal error" in body or "PHP Warning" in body or "PHP Notice" in body:
                return "PLUGIN_CONFLICT"
            return "UNKNOWN"

    return "UNKNOWN"


def owner_message_for_error(error_code: str) -> str:
    """Return the plain-language owner-facing message for an error code."""
    return _ERROR_OWNER_MESSAGES.get(error_code, _ERROR_OWNER_MESSAGES["UNKNOWN"])


# ---------------------------------------------------------------------------
# §6.1  Idempotency
# ---------------------------------------------------------------------------


class IdempotencyChecker:
    """Check and record processed messages via the processed_messages table.

    Usage::

        checker = IdempotencyChecker(db_path)
        if not checker.is_new(tenant_id, provider_message_id):
            return  # duplicate — skip everything
        # ... process the message ...
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def is_new(self, tenant_id: str, provider_message_id: str) -> bool:
        """Return True if this is a NEW message (not a duplicate).

        Must be called BEFORE any AI call to avoid burning quota on
        duplicates (§6.1).
        """
        return mark_message_processed(self._db_path, tenant_id, provider_message_id)


# ---------------------------------------------------------------------------
# §6.2  Rate limiting (DB-backed)
# ---------------------------------------------------------------------------


class DBRateLimiter:
    """Fixed-window rate limiter backed by the rate_limit_buckets table.

    Replaces the in-memory RateLimiter for production use.  The in-memory
    version is kept for tests and single-worker dev mode.

    Usage::

        limiter = DBRateLimiter(db_path)
        is_limited, count = limiter.check(tenant_id)
        if is_limited:
            # reply with rate-limit notice, drop the message
    """

    def __init__(
        self,
        db_path: Path,
        max_messages: int = 30,
        window_hours: int = 1,
    ) -> None:
        self._db_path = db_path
        self._max_messages = max_messages
        self._window_hours = window_hours

    def check(self, tenant_id: str) -> tuple[bool, int]:
        """Check and increment the rate limit counter.

        Returns (is_limited, current_count).
        If is_limited is True, the caller should drop the message and
        reply with the rate-limit notice (§6.2).
        """
        return check_and_increment_rate_limit(
            self._db_path,
            tenant_id,
            max_messages=self._max_messages,
            window_hours=self._window_hours,
        )


# ---------------------------------------------------------------------------
# §6.3  Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Wraps Track B calls with retry, backoff, and tenant status management.

    Per §6.3:
    - On failure: retry up to 3 times with exponential backoff (1s, 3s, 9s)
    - On final failure: set tenant.status = 'degraded'
    - On next successful call: auto-recover to 'active'
    - Operator alert on transition to degraded (§7.4)

    Usage::

        breaker = CircuitBreaker(db_path, tenant_id, alert_fn=send_alert)
        result = await breaker.call(lambda: trackb.submit_intent(intent))
    """

    def __init__(
        self,
        db_path: Path,
        tenant_id: str,
        *,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 9.0,
        alert_fn: Any = None,
    ) -> None:
        self._db_path = db_path
        self._tenant_id = tenant_id
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._alert_fn = alert_fn

    async def call(self, coro_fn: Any) -> Any:
        """Execute ``coro_fn()`` with retry + circuit breaker logic.

        On success: auto-recover tenant if degraded.
        On final failure: set tenant to degraded, fire operator alert.
        """
        from .retry import retry_with_backoff

        try:
            result = await retry_with_backoff(
                coro_fn,
                max_attempts=self._max_attempts,
                base_delay=self._base_delay,
                max_delay=self._max_delay,
                operation_name=f"circuit_breaker(tenant={self._tenant_id})",
            )
            # Auto-recover on success (§6.3)
            self._clear_degraded()
            return result
        except Exception as exc:
            # Check current status BEFORE setting degraded, so we can
            # detect the transition for the operator alert (§7.4).
            was_degraded = self._is_degraded()
            error_code = classify_wp_error(exc)
            self._set_degraded()
            # Fire operator alert on transition to degraded — not on
            # repeated failures of an already-degraded tenant.
            if not was_degraded:
                self._alert(error_code, exc)
            # Re-raise so the caller can map the error code to an
            # owner-facing message
            raise CircuitBreakerError(error_code, exc) from exc

    def _is_degraded(self) -> bool:
        """Check if tenant is currently degraded."""
        tenant = get_tenant(self._db_path, self._tenant_id)
        return tenant is not None and tenant["status"] == "degraded"

    def _set_degraded(self) -> None:
        """Set tenant status to 'degraded' (idempotent)."""
        tenant = get_tenant(self._db_path, self._tenant_id)
        if tenant and tenant["status"] != "degraded":
            update_tenant_status(self._db_path, self._tenant_id, "degraded")
            logger.warning(
                "tenant %s marked degraded (circuit breaker tripped)",
                self._tenant_id,
            )

    def _clear_degraded(self) -> None:
        """Auto-recover tenant to 'active' on successful WP call (§6.3)."""
        tenant = get_tenant(self._db_path, self._tenant_id)
        if tenant and tenant["status"] == "degraded":
            update_tenant_status(self._db_path, self._tenant_id, "active")
            logger.info(
                "tenant %s recovered to active (successful WP call)",
                self._tenant_id,
            )

    def _alert(self, error_code: str, exc: Exception) -> None:
        """Fire operator alert on transition to degraded (§7.4).

        Called only when transitioning FROM non-degraded TO degraded.
        The alert_fn callback sends a Telegram message to the operator.
        """
        if self._alert_fn is not None:
            try:
                self._alert_fn(
                    f"⚠️ Tenant {self._tenant_id} degraded: "
                    f"error_code={error_code}, detail={exc!s:.200}"
                )
            except Exception:
                logger.exception("Failed to send operator alert")


class CircuitBreakerError(Exception):
    """Raised when the circuit breaker trips after exhausting retries."""

    def __init__(self, error_code: str, original: Exception) -> None:
        self.error_code = error_code
        self.original = original
        self.owner_message = owner_message_for_error(error_code)
        super().__init__(
            f"Circuit breaker tripped: {error_code} — {original}"
        )


# ---------------------------------------------------------------------------
# Combined reliability layer
# ---------------------------------------------------------------------------


class ReliabilityLayer:
    """Combines idempotency, rate limiting, and circuit breaker.

    Wired into the FastAPI app at startup.  The webhook handler calls
    ``check_idempotency`` and ``check_rate_limit`` before processing;
    the routing layer uses ``circuit_breaker`` around Track B calls.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_messages: int = 30,
        window_hours: int = 1,
        alert_fn: Any = None,
    ) -> None:
        self.db_path = db_path
        self.idempotency = IdempotencyChecker(db_path)
        self.rate_limiter = DBRateLimiter(db_path, max_messages, window_hours)
        self._alert_fn = alert_fn

    def check_idempotency(self, tenant_id: str, provider_message_id: str) -> bool:
        """Return True if this is a NEW message (not a duplicate)."""
        return self.idempotency.is_new(tenant_id, provider_message_id)

    def check_rate_limit(self, tenant_id: str) -> tuple[bool, int]:
        """Check and increment the rate limit counter."""
        return self.rate_limiter.check(tenant_id)

    def circuit_breaker(self, tenant_id: str) -> CircuitBreaker:
        """Create a circuit breaker for a specific tenant's WP calls."""
        return CircuitBreaker(
            self.db_path,
            tenant_id,
            alert_fn=self._alert_fn,
        )
