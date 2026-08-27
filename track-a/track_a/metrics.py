"""Prometheus-style metrics collector for Track A.

Exposes a ``GET /metrics`` endpoint in Prometheus text format.  The
collector is a singleton that tracks the key counters for operational
visibility:

- messages received / duplicates / rate-limited
- intents parsed (by outcome: success, low_confidence, unsupported)
- Track B submits (success / failure)
- LLM calls, retries, and rate-limit events

Usage::

    from track_a.metrics import metrics
    metrics.messages_received += 1
    # ... in a route ...
    @app.get("/metrics")
    async def metrics_endpoint():
        return PlainTextResponse(metrics.render(), media_type="text/plain")
"""

from __future__ import annotations

import threading
import time
from typing import Any


class MetricsCollector:
    """Thread-safe counter store with Prometheus text rendering."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.time()
        # --- message pipeline ---
        self.messages_received: int = 0
        self.messages_duplicate: int = 0
        self.messages_rate_limited: int = 0
        # --- intent parsing ---
        self.intents_parsed: int = 0
        self.intents_success: int = 0
        self.intents_low_confidence: int = 0
        self.intents_unsupported: int = 0
        # --- Track B ---
        self.trackb_submits: int = 0
        self.trackb_successes: int = 0
        self.trackb_failures: int = 0
        # --- LLM ---
        self.llm_calls: int = 0
        self.llm_retries: int = 0
        self.llm_rate_limited: int = 0

    def inc(self, name: str, value: int = 1) -> None:
        """Increment a counter by ``value`` (thread-safe)."""
        with self._lock:
            current = getattr(self, name, None)
            if current is not None and isinstance(current, int):
                setattr(self, name, current + value)

    def render(self) -> str:
        """Render all counters in Prometheus exposition text format."""
        lines: list[str] = []
        ts = int(time.time() * 1000)

        def _add(name: str, help_text: str, value: int) -> None:
            lines.append(f"# HELP wpbot_{name} {help_text}")
            lines.append(f"# TYPE wpbot_{name} counter")
            lines.append(f"wpbot_{name} {value} {ts}")

        with self._lock:
            _add("messages_received", "Total inbound WhatsApp messages received", self.messages_received)
            _add("messages_duplicate", "Duplicate webhook deliveries skipped", self.messages_duplicate)
            _add("messages_rate_limited", "Messages rejected by per-owner rate limiter", self.messages_rate_limited)
            _add("intents_parsed", "Total intents parsed by LLM", self.intents_parsed)
            _add("intents_success", "Intents that passed validation and routing", self.intents_success)
            _add("intents_low_confidence", "Intents that fell to low-confidence path", self.intents_low_confidence)
            _add("intents_unsupported", "Out-of-scope requests escalated", self.intents_unsupported)
            _add("trackb_submits", "Total Track B submit/undo calls", self.trackb_submits)
            _add("trackb_successes", "Track B calls returning success", self.trackb_successes)
            _add("trackb_failures", "Track B calls returning failure", self.trackb_failures)
            _add("llm_calls", "Total LLM API calls", self.llm_calls)
            _add("llm_retries", "LLM calls retried (malformed JSON)", self.llm_retries)
            _add("llm_rate_limited", "LLM calls that hit rate limits", self.llm_rate_limited)

        # Uptime gauge
        uptime = int(time.time() - self._started)
        lines.append("# HELP wpbot_uptime_seconds Process uptime in seconds")
        lines.append("# TYPE wpbot_uptime_seconds gauge")
        lines.append(f"wpbot_uptime_seconds {uptime} {ts}")

        return "\n".join(lines) + "\n"


# Module-level singleton — import and use anywhere.
metrics = MetricsCollector()
