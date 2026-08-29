"""Simple sliding-window rate limiter for the webhook endpoint.

Protects the LLM and Track B pipeline from bursts of messages from a
single owner (misconfigured Meta delivery or abuse).  The limiter is
per-owner and uses an in-memory dict — appropriate for a single-worker
deployment.  A multi-worker deployment should swap this for Redis-backed
rate limiting.

Usage::

    limiter = RateLimiter(max_requests=30, window_seconds=60)
    if limiter.is_rate_limited(owner_phone):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window rate limiter keyed by an arbitrary string (owner id)."""

    def __init__(
        self,
        max_requests: int = 30,
        window_seconds: float = 60.0,
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._windows: dict[str, deque[float]] = {}
        self._last_cleanup = time.monotonic()
        self._lock = threading.Lock()

    def is_rate_limited(self, key: str) -> bool:
        """Return True if the key has exceeded the rate limit."""
        now = time.monotonic()
        with self._lock:
            self._maybe_cleanup(now)

            dq = self._windows.get(key)
            if dq is None:
                dq = deque()
                self._windows[key] = dq

            # Evict entries outside the window.
            cutoff = now - self._window
            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) >= self._max:
                return True

            dq.append(now)
            return False

    def _maybe_cleanup(self, now: float) -> None:
        """Periodically evict empty or fully-expired keys."""
        if now - self._last_cleanup < self._window:
            return
        self._last_cleanup = now
        cutoff = now - self._window
        stale = [
            k
            for k, dq in self._windows.items()
            if not dq or dq[-1] <= cutoff
        ]
        for k in stale:
            del self._windows[k]
