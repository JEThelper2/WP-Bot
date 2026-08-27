"""Retry utility with exponential backoff and jitter.

Used for transient failures on Track B calls and LLM retries.
The caller provides an async callable; this module handles the retry
loop with configurable attempts, base delay, max delay, and jitter.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

logger = logging.getLogger("track_a.retry")

T = TypeVar("T")


async def retry_with_backoff(
    coro_fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    operation_name: str = "operation",
) -> T:
    """Execute ``coro_fn()`` with exponential backoff on retryable errors.

    Parameters
    ----------
    coro_fn:
        Zero-argument async callable that returns the result.
    max_attempts:
        Total attempts (including the first one).  ``max_attempts=1``
        means no retry.
    base_delay:
        Initial delay in seconds before the first retry.
    max_delay:
        Maximum delay cap (before jitter is applied).
    retryable_exceptions:
        Exception types that trigger a retry.  Others propagate immediately.
    operation_name:
        Human-readable name for log messages.

    Returns the result of the first successful call.  Raises the last
    exception if all attempts fail.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn()
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == max_attempts:
                logger.warning(
                    "%s failed after %d attempts: %s",
                    operation_name,
                    attempt,
                    exc,
                )
                raise
            # Exponential backoff with full jitter.
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jittered = random.uniform(0, delay)
            logger.info(
                "%s failed (attempt %d/%d): %s — retrying in %.2fs",
                operation_name,
                attempt,
                max_attempts,
                exc,
                jittered,
            )
            await asyncio.sleep(jittered)
    # Should not reach here, but satisfy the type checker.
    assert last_exc is not None  # noqa: S101
    raise last_exc
