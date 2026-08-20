"""Provider-agnostic LLM interface for intent parsing.

Defines the ``AIProvider`` Protocol that the intent parser depends on
(Dependency Inversion). Concrete providers implement this interface; the
``IntentParser`` never knows which backend it's talking to.

Provider selection happens at startup via the ``AI_PROVIDER`` env var
and the provider's own API key env var (e.g. ``GROQ_API_KEY``). The
application wires the correct provider into ``IntentParser`` — no
business logic references a concrete provider by name.

Adding a new provider (e.g. OpenAI, Claude) means:
1. Write one class implementing ``AIProvider``.
2. Add one branch to ``get_provider()``.
3. No changes to ``IntentParser``, no changes to existing tests.

The ``RetryableProvider`` wrapper adds:
- One retry on malformed JSON (free-tier models drift more often).
- Timeout with fallback to low-confidence (the pipeline never crashes).

The ``FallbackChain`` wrapper adds:
- Per-request failover to a secondary provider on rate-limit (429).
- Non-rate-limit errors stay within the same provider (no failover).
- Every fallback event is logged for capacity planning.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Protocol

logger = logging.getLogger("track_a.ai_provider")


# ---------------------------------------------------------------------------
# Provider interface (Dependency Inversion)
# ---------------------------------------------------------------------------

class AIProvider(Protocol):
    """Single method the intent parser depends on.

    Implementations handle their own auth, API calls, and JSON parsing.
    The caller never sees provider-specific details.
    """

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        """Send a system+user prompt pair and return parsed JSON.

        Must return a dict.  Raise on transport errors, auth failures,
        or malformed responses — the caller (or a retry wrapper) decides
        what to do with failures.
        """
        ...


# ---------------------------------------------------------------------------
# Groq provider (default — free, fast, OpenAI-compatible)
# ---------------------------------------------------------------------------

class GroqProvider:
    """Groq's OpenAI-compatible API for structured JSON extraction.

    Uses the ``openai`` library with a custom ``base_url`` pointing at
    Groq's endpoint.  This keeps the dependency surface minimal (no
    separate ``groq`` SDK) and makes future OpenAI swaps trivial.

    Env vars:
        GROQ_API_KEY  — required (Groq Cloud API key)
        GROQ_MODEL    — optional, defaults to ``openai/gpt-oss-120b``
    """

    BASE_URL = "https://api.groq.com/openai/v1"
    DEFAULT_MODEL = "openai/gpt-oss-120b"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: Any = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("GROQ_API_KEY")
        self._model = model or os.environ.get("GROQ_MODEL", self.DEFAULT_MODEL)
        self._client = client  # injected for testing

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        if not self._api_key:
            raise ValueError(
                "GROQ_API_KEY is not configured; cannot call the LLM"
            )

        from openai import AsyncOpenAI

        client = self._client or AsyncOpenAI(
            api_key=self._api_key,
            base_url=self.BASE_URL,
        )
        response = await client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)


# ---------------------------------------------------------------------------
# Retry + timeout wrapper (applies to any provider)
# ---------------------------------------------------------------------------

class RateLimitError(Exception):
    """Raised when a provider returns HTTP 429 (rate limited).

    This is NOT retried by ``RetryableProvider`` — it propagates to
    ``FallbackChain`` which may try a secondary provider.  Non-rate-limit
    errors (malformed JSON, timeouts) stay within the same provider's
    retry logic.
    """


# Sentinel to detect provider-specific rate limit exceptions.
# Groq's OpenAI SDK raises openai.RateLimitError; we catch that
# and re-raise as our own RateLimitError for provider-agnostic handling.


def _is_rate_limit(exc: Exception) -> bool:
    """Check if an exception is a rate-limit response from any provider."""
    # Our own RateLimitError
    if isinstance(exc, RateLimitError):
        return True
    # OpenAI SDK (used by Groq) raises openai.RateLimitError
    try:
        import openai
        if isinstance(exc, openai.RateLimitError):
            return True
    except ImportError:
        pass
    # Google SDK raises google.api_core.exceptions.ResourceExhausted
    try:
        from google.api_core import exceptions as google_exc
        if isinstance(exc, google_exc.ResourceExhausted):
            return True
    except ImportError:
        pass
    # Fallback: check status_code attribute (httpx-style)
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    return False


class RetryableProvider:
    """Wraps an ``AIProvider`` with retry-on-malformed-JSON and timeout.

    - On malformed JSON or non-dict output: retries once with a
      corrective follow-up prompt.
    - On rate-limit (429): re-raises immediately so ``FallbackChain``
      can try a secondary provider — does NOT retry on the same provider.
    - On timeout or any other exception after retry: raises so the caller
      (``IntentParser.parse``) can return low-confidence.

    This is especially important for free-tier models which are more
    prone to occasional format drift.
    """

    # Timeout for a single LLM call (seconds).
    TIMEOUT = 15.0

    def __init__(self, inner: AIProvider) -> None:
        self._inner = inner

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        first_exc: Exception | None = None

        for attempt in range(2):
            try:
                result = await asyncio.wait_for(
                    self._complete_once(system=system, user=user),
                    timeout=self.TIMEOUT,
                )
                return result
            except asyncio.TimeoutError as exc:
                logger.warning("LLM call timed out (attempt %d/2)", attempt + 1)
                first_exc = first_exc or exc
            except json.JSONDecodeError as exc:
                logger.warning(
                    "LLM returned malformed JSON (attempt %d/2): %s",
                    attempt + 1,
                    exc,
                )
                first_exc = first_exc or exc
                # Retry with a corrective follow-up
                user = (
                    f"{user}\n\n"
                    "IMPORTANT: Your previous response was not valid JSON. "
                    "Reply with EXACTLY ONE JSON object and nothing else."
                )
            except RateLimitError:
                # Rate limits are NOT retried — they propagate to
                # FallbackChain for per-request failover.
                raise
            except Exception as exc:
                # Check if this is a provider-specific rate limit
                if _is_rate_limit(exc):
                    raise RateLimitError(str(exc)) from exc
                logger.warning(
                    "LLM output parsing failed (attempt %d/2): %s",
                    attempt + 1,
                    exc,
                )
                first_exc = first_exc or exc

        # All retries exhausted — raise the original exception so
        # IntentParser can return low_confidence.
        raise first_exc  # type: ignore[misc]

    async def _complete_once(self, *, system: str, user: str) -> dict[str, Any]:
        result = await self._inner.complete_json(system=system, user=user)
        if not isinstance(result, dict):
            raise ValueError(f"Expected dict, got {type(result).__name__}")
        return result


# ---------------------------------------------------------------------------
# Gemini provider (fallback — free tier, acceptable for overflow)
# ---------------------------------------------------------------------------

class GeminiProvider:
    """Google Gemini API (Flash) for structured JSON extraction.

    Uses the ``google-genai`` SDK.  Gemini Flash is the fallback provider,
    NOT the primary.  Reason: Groq is faster (LPU-based inference), and
    Gemini's free-tier terms currently permit using submitted prompts for
    model training — acceptable for occasional overflow traffic, not
    ideal as the default path for real business data.

    Env vars:
        GEMINI_API_KEY  — required (Google AI Studio API key)
        GEMINI_MODEL    — optional, defaults to ``gemini-2.5-flash``
    """

    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: Any = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self._model = model or os.environ.get("GEMINI_MODEL", self.DEFAULT_MODEL)
        self._client = client  # injected for testing

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        if not self._api_key:
            raise ValueError(
                "GEMINI_API_KEY is not configured; cannot call the LLM"
            )

        from google import genai
        from google.genai import types

        client = self._client or genai.Client(api_key=self._api_key)
        response = await client.aio.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        text = response.text or "{}"
        return json.loads(text)


# ---------------------------------------------------------------------------
# Fallback chain (per-request failover on rate limit)
# ---------------------------------------------------------------------------

class FallbackChain:
    """Provider chain that tries primary first, falls back on rate limit.

    On rate-limit (429) from the primary, attempts the fallback provider
    for that single request only — does NOT switch the primary for
    subsequent requests.  If both fail, raises the last exception so
    ``IntentParser`` returns low-confidence.

    Every fallback event is logged with provider names and reason for
    capacity planning (a pattern of frequent fallbacks is the trigger
    for upgrading to Groq's Developer tier).
    """

    def __init__(self, primary: AIProvider, fallback: AIProvider) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_name = type(primary).__name__
        self._fallback_name = type(fallback).__name__

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        try:
            return await self._primary.complete_json(system=system, user=user)
        except RateLimitError as exc:
            logger.warning(
                "Provider %s rate-limited, falling back to %s: %s",
                self._primary_name,
                self._fallback_name,
                exc,
            )
            try:
                result = await self._fallback.complete_json(
                    system=system, user=user
                )
                logger.info(
                    "Fallback to %s succeeded for this request",
                    self._fallback_name,
                )
                return result
            except RateLimitError as fallback_exc:
                logger.error(
                    "Both %s and %s rate-limited; failing over to low-confidence",
                    self._primary_name,
                    self._fallback_name,
                )
                raise fallback_exc from exc
            except Exception as fallback_exc:
                logger.error(
                    "Fallback %s also failed: %s",
                    self._fallback_name,
                    fallback_exc,
                )
                raise fallback_exc from exc


# ---------------------------------------------------------------------------
# Provider factory (startup wiring only)
# ---------------------------------------------------------------------------

# Registry of provider constructors keyed by the env value.
_PROVIDERS: dict[str, type[AIProvider]] = {
    "groq": GroqProvider,
    "gemini": GeminiProvider,
}


def _build_provider(name: str, **kwargs: Any) -> AIProvider:
    """Build a single provider by name (no wrapping)."""
    cls = _PROVIDERS.get(name)
    if cls is None:
        available = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"Unknown AI provider {name!r}; available: {available}"
        )
    return cls(**kwargs)


def get_provider(
    name: str | None = None,
    fallback_name: str | None = None,
    **kwargs: Any,
) -> AIProvider:
    """Instantiate the selected AI provider with optional fallback.

    ``name`` defaults to the ``AI_PROVIDER`` env var, falling back to
    ``"groq"``.  ``fallback_name`` defaults to ``AI_FALLBACK_PROVIDER``
    env var (empty string = no fallback).

    Extra ``**kwargs`` are forwarded to the primary provider constructor.
    The fallback provider reads its own key from the environment.

    The returned provider is wrapped in:
    1. ``RetryableProvider`` — retry-on-malformed-JSON + timeout.
    2. ``FallbackChain`` (if a fallback is configured) — per-request
       failover on rate limit.
    """
    name = (name or os.environ.get("AI_PROVIDER") or "groq").lower()
    fallback_name = (
        fallback_name or os.environ.get("AI_FALLBACK_PROVIDER") or ""
    ).lower()

    # Build the primary provider, wrapped in RetryableProvider.
    primary = RetryableProvider(_build_provider(name, **kwargs))

    if not fallback_name:
        return primary

    # Build the fallback provider, also wrapped in RetryableProvider.
    fallback = RetryableProvider(_build_provider(fallback_name))
    return FallbackChain(primary=primary, fallback=fallback)


def register_provider(name: str, cls: type[AIProvider]) -> None:
    """Register a new AI provider for use with ``get_provider()``.

    Call this from a third-party module to add a provider without
    modifying this file (Open/Closed Principle).
    """
    _PROVIDERS[name] = cls
