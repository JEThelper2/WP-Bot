"""Operator alerting via Telegram (§7.4).

Sends alerts to the operator's personal Telegram chat when:
- A tenant transitions to 'degraded' (circuit breaker tripped)
- An unhandled exception occurs in the request path
- AI provider fallback triggers more than 5 times in a rolling hour

The alert uses the same Telegram Bot API infrastructure already built
for Track A's Telegram adapter — no new service to manage.

Design:
- Single `TelegramAlertSender` class that sends messages via Bot API
- Alert callback wired into `CircuitBreaker` and `ReliabilityLayer`
- Fallback frequency tracking for AI provider monitoring
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

import httpx

logger = logging.getLogger("track_a.alerting")


class TelegramAlertSender:
    """Send operator alerts via Telegram Bot API (§7.4).

    Uses the same bot token as the Telegram adapter. Alerts go to
    the operator's personal chat (set via TELEGRAM_OPERATOR_CHAT_ID).

    Usage::

        sender = TelegramAlertSender(bot_token="...", chat_id="...")
        sender("⚠️ Tenant degraded: ...")
    """

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._client = client

    def __call__(self, message: str) -> None:
        """Send an alert. Sync wrapper for use as a callback.

        The circuit breaker calls this synchronously, so we use
        fire-and-forget with httpx's sync client.
        """
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram alerts not configured; alert: %s", message)
            return

        try:
            import httpx as _httpx

            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": message}
            # Fire and forget — don't block the request path
            with _httpx.Client(timeout=10.0) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                logger.info("Operator alert sent via Telegram")
        except Exception as exc:
            logger.error("Failed to send operator alert: %s", exc)


class FallbackFrequencyTracker:
    """Track AI provider fallback frequency (§7.4).

    Fires an operator alert if fallback triggers more than
    `threshold` times in a rolling `window_seconds` period.
    This is the leading indicator that the Groq free tier needs
    to move to the paid Developer tier.
    """

    def __init__(
        self,
        threshold: int = 5,
        window_seconds: int = 3600,
        alert_fn: Any = None,
    ) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self._alert_fn = alert_fn
        self._timestamps: deque[float] = deque()

    def record_fallback(self) -> None:
        """Record a fallback event and alert if threshold exceeded."""
        now = time.monotonic()
        self._timestamps.append(now)

        # Prune old entries outside the window
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

        if len(self._timestamps) > self.threshold and self._alert_fn is not None:
            try:
                self._alert_fn(
                    f"⚠️ AI provider fallback triggered {len(self._timestamps)} "
                    f"times in the last hour. Consider upgrading Groq to paid tier."
                )
            except Exception:
                logger.exception("Failed to send fallback frequency alert")

    @property
    def count(self) -> int:
        """Current fallback count in the rolling window."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        return len(self._timestamps)
