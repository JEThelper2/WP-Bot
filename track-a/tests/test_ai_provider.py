"""AI provider tests: GroqProvider, RetryableProvider, FallbackChain, provider swap.

Tests cover:
- GroqProvider correctly calls the OpenAI-compatible API with Groq's endpoint.
- RetryableProvider retries once on malformed JSON.
- RetryableProvider raises on timeout so IntentParser returns low-confidence.
- RetryableProvider re-raises RateLimitError (not retryable).
- FallbackChain: primary success never touches fallback.
- FallbackChain: 429 triggers fallback and returns valid result.
- FallbackChain: both providers fail → low-confidence.
- FallbackChain: non-429 error does NOT trigger failover.
- get_provider() factory wires the correct provider from config.
- The pipeline works with ANY conforming AIProvider (provider swap test).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from track_a.ai_provider import (
    FallbackChain,
    GeminiProvider,
    GroqProvider,
    RateLimitError,
    RetryableProvider,
    get_provider,
    register_provider,
)
from track_a.intent import IntentParser, IntentParseResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OWNER = "15551234567"


class FakeProvider:
    """Minimal AIProvider implementation for testing."""

    def __init__(self, script: dict[str, dict]) -> None:
        self.script = script
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        if user not in self.script:
            raise AssertionError(f"no scripted response for: {user!r}")
        return self.script[user]


class ExplodingProvider:
    """Always raises — tests timeout/error fallback."""

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        raise RuntimeError("provider exploded")


class MalformedProvider:
    """Returns non-dict on first call, correct on second (retry test)."""

    def __init__(self, bad: Any, good: dict[str, Any]) -> None:
        self._bad = bad
        self._good = good
        self._calls = 0

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        self._calls += 1
        if self._calls == 1:
            return self._bad  # type: ignore[return-value]
        return self._good


class SlowProvider:
    """Responds after a configurable delay (timeout test)."""

    def __init__(self, delay: float = 10.0) -> None:
        self._delay = delay

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        await asyncio.sleep(self._delay)
        return {
            "action": "update",
            "content_type": "business_info",
            "fields": {"hours": "9-6"},
            "confidence": 0.9,
        }


class RateLimitedProvider:
    """Always raises RateLimitError (simulates 429)."""

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        raise RateLimitError("429 rate limited")


class FailingThenSuccessProvider:
    """Fails on first call, succeeds on second (fallback chain test)."""

    def __init__(self, fail_exc: Exception, success: dict[str, Any]) -> None:
        self._fail_exc = fail_exc
        self._success = success
        self._calls = 0

    async def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        self._calls += 1
        if self._calls == 1:
            raise self._fail_exc
        return self._success


def _run_parse(parser: IntentParser, text: str) -> IntentParseResult:
    return asyncio.run(parser.parse(text, OWNER))


# ---------------------------------------------------------------------------
# GroqProvider tests
# ---------------------------------------------------------------------------


class TestGroqProvider:
    """GroqProvider calls the OpenAI-compatible API with Groq's endpoint."""

    def test_missing_api_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            provider = GroqProvider(api_key=None)
            with pytest.raises(ValueError, match="GROQ_API_KEY"):
                asyncio.run(provider.complete_json(system="sys", user="msg"))

    def test_calls_correct_endpoint(self) -> None:
        """Verify the client is configured with Groq's base URL."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = json.dumps(
            {
                "action": "create",
                "content_type": "job",
                "fields": {"title": "Test"},
                "confidence": 0.9,
            }
        )
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        provider = GroqProvider(api_key="test-key", client=mock_client)
        asyncio.run(provider.complete_json(system="sys", user="msg"))

        # Verify the client was called with the right arguments
        mock_client.chat.completions.create.assert_called_once()
        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == "openai/gpt-oss-120b"
        assert call_kwargs.kwargs["response_format"] == {"type": "json_object"}
        assert call_kwargs.kwargs["temperature"] == 0.0
        # Verify the messages structure
        messages = call_kwargs.kwargs["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_custom_model_override(self) -> None:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "{}"
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        provider = GroqProvider(api_key="test-key", model="custom-model", client=mock_client)
        asyncio.run(provider.complete_json(system="sys", user="msg"))

        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == "custom-model"


# ---------------------------------------------------------------------------
# RateLimitError tests
# ---------------------------------------------------------------------------


class TestRateLimitError:
    """RateLimitError is detected and re-raised by RetryableProvider."""

    def test_rate_limit_error_not_retried(self) -> None:
        """RetryableProvider does NOT retry on rate limit — it propagates."""
        inner = RateLimitedProvider()
        provider = RetryableProvider(inner)
        with pytest.raises(RateLimitError, match="429"):
            asyncio.run(provider.complete_json(system="sys", user="msg"))

    def test_is_rate_limit_detects_openai_error(self) -> None:
        """_is_rate_limit detects openai.RateLimitError."""
        from track_a.ai_provider import _is_rate_limit

        # Simulate openai.RateLimitError without importing openai
        class FakeOpenAIRateLimitError(Exception):
            status_code = 429

        exc = FakeOpenAIRateLimitError("rate limited")
        # The function checks isinstance, so this won't match the real class
        # but it will match the status_code fallback
        assert _is_rate_limit(exc) is True

    def test_is_rate_limit_rejects_other_errors(self) -> None:
        """_is_rate_limit returns False for non-rate-limit errors."""
        from track_a.ai_provider import _is_rate_limit

        assert _is_rate_limit(RuntimeError("something else")) is False
        assert _is_rate_limit(ValueError("bad value")) is False


# ---------------------------------------------------------------------------
# RetryableProvider tests
# ---------------------------------------------------------------------------


class TestRetryableProvider:
    """RetryableProvider adds retry and timeout handling."""

    def test_retries_on_malformed_json(self) -> None:
        """Non-dict output triggers a retry with corrective prompt."""
        bad_output = ["not", "a", "dict"]
        good_output = {
            "action": "update",
            "content_type": "business_info",
            "fields": {"hours": "9-6"},
            "confidence": 0.9,
        }

        inner = MalformedProvider(bad_output, good_output)
        provider = RetryableProvider(inner)
        result = asyncio.run(provider.complete_json(system="sys", user="msg"))
        assert result == good_output
        # The corrective prompt should have been appended
        assert inner._calls == 2

    def test_timeout_raises_error(self) -> None:
        """Timeout raises so IntentParser can return low_confidence."""
        # Use a very short timeout for testing
        provider = RetryableProvider(SlowProvider(delay=10.0))
        provider.TIMEOUT = 0.1  # Override for test
        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(provider.complete_json(system="sys", user="msg"))

    def test_exception_propagates_after_retry(self) -> None:
        """If retry also fails, the original exception propagates."""
        provider = RetryableProvider(ExplodingProvider())
        with pytest.raises(RuntimeError, match="provider exploded"):
            asyncio.run(provider.complete_json(system="sys", user="msg"))

    def test_valid_json_passes_through(self) -> None:
        """Valid dict output passes through without retry."""
        good = {
            "action": "create",
            "content_type": "job",
            "fields": {"title": "Test", "description": "Desc"},
            "confidence": 0.9,
        }
        inner = FakeProvider({"msg": good})
        provider = RetryableProvider(inner)
        result = asyncio.run(provider.complete_json(system="sys", user="msg"))
        assert result == good
        assert len(inner.calls) == 1  # No retry


# ---------------------------------------------------------------------------
# FallbackChain tests
# ---------------------------------------------------------------------------


class TestFallbackChain:
    """FallbackChain tries primary first, falls back on rate limit."""

    def test_primary_success_never_touches_fallback(self) -> None:
        """When primary succeeds, fallback is never called."""
        good = {
            "action": "create",
            "content_type": "job",
            "fields": {"title": "Test", "description": "Desc"},
            "confidence": 0.9,
        }
        primary = FakeProvider({"msg": good})
        fallback = FakeProvider({"msg": good})
        chain = FallbackChain(primary=primary, fallback=fallback)
        result = asyncio.run(chain.complete_json(system="sys", user="msg"))
        assert result == good
        assert len(primary.calls) == 1
        assert len(fallback.calls) == 0  # Never touched

    def test_429_triggers_fallback_and_succeeds(self) -> None:
        """Rate limit on primary triggers fallback which returns valid result."""
        good = {
            "action": "update",
            "content_type": "business_info",
            "fields": {"hours": "9-6"},
            "confidence": 0.9,
        }
        primary = RateLimitedProvider()
        fallback = FakeProvider({"msg": good})
        chain = FallbackChain(primary=primary, fallback=fallback)
        result = asyncio.run(chain.complete_json(system="sys", user="msg"))
        assert result == good
        assert len(fallback.calls) == 1

    def test_both_providers_rate_limited_raises(self) -> None:
        """If both primary and fallback are rate-limited, exception propagates."""
        primary = RateLimitedProvider()
        fallback = RateLimitedProvider()
        chain = FallbackChain(primary=primary, fallback=fallback)
        with pytest.raises(RateLimitError):
            asyncio.run(chain.complete_json(system="sys", user="msg"))

    def test_fallback_other_error_also_propagates(self) -> None:
        """If fallback fails with non-rate-limit error, it propagates."""
        primary = RateLimitedProvider()
        fallback = ExplodingProvider()
        chain = FallbackChain(primary=primary, fallback=fallback)
        with pytest.raises(RuntimeError, match="provider exploded"):
            asyncio.run(chain.complete_json(system="sys", user="msg"))

    def test_non_429_error_does_not_trigger_failover(self) -> None:
        """Non-rate-limit errors stay within same provider (no failover)."""
        good = {
            "action": "create",
            "content_type": "job",
            "fields": {"title": "Test", "description": "Desc"},
            "confidence": 0.9,
        }
        # Primary raises a non-429 error (RuntimeError)
        primary = ExplodingProvider()
        fallback = FakeProvider({"msg": good})
        chain = FallbackChain(primary=primary, fallback=fallback)
        # Should NOT fall back — RuntimeError is not a rate limit
        with pytest.raises(RuntimeError, match="provider exploded"):
            asyncio.run(chain.complete_json(system="sys", user="msg"))
        # Fallback was never called
        assert len(fallback.calls) == 0

    def test_fallback_is_per_request_not_persistent(self) -> None:
        """Fallback is per-request: next request goes back to primary."""
        good = {
            "action": "create",
            "content_type": "job",
            "fields": {"title": "Test", "description": "Desc"},
            "confidence": 0.9,
        }
        # Primary: rate-limited on first call, succeeds on second
        primary = FailingThenSuccessProvider(RateLimitError("429"), good)
        fallback = FakeProvider({"msg": good})
        chain = FallbackChain(primary=primary, fallback=fallback)

        # First request: primary rate-limited → fallback
        result1 = asyncio.run(chain.complete_json(system="sys", user="msg"))
        assert result1 == good
        assert primary._calls == 1
        assert len(fallback.calls) == 1

        # Second request: primary succeeds (no fallback)
        result2 = asyncio.run(chain.complete_json(system="sys", user="msg"))
        assert result2 == good
        assert primary._calls == 2
        assert len(fallback.calls) == 1  # Still 1 — not called again

    def test_chain_works_through_intent_parser(self) -> None:
        """FallbackChain integrates correctly with IntentParser."""
        good = {
            "action": "update",
            "content_type": "business_info",
            "fields": {"hours": "9-6"},
            "confidence": 0.95,
        }
        primary = RateLimitedProvider()
        fallback = FakeProvider({"change my hours to 9-6": good})
        chain = FallbackChain(primary=primary, fallback=fallback)
        parser = IntentParser(llm=chain)
        result = _run_parse(parser, "change my hours to 9-6")
        assert result.status == "intent"
        assert result.intent is not None
        assert result.intent["fields"]["hours"] == "9-6"


# ---------------------------------------------------------------------------
# GeminiProvider tests
# ---------------------------------------------------------------------------


class TestGeminiProvider:
    """GeminiProvider uses Google's Gemini API."""

    def test_missing_api_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            provider = GeminiProvider(api_key=None)
            with pytest.raises(ValueError, match="GEMINI_API_KEY"):
                asyncio.run(provider.complete_json(system="sys", user="msg"))

    def test_uses_correct_model(self) -> None:
        """Verify the provider uses Gemini Flash by default."""
        assert GeminiProvider.DEFAULT_MODEL == "gemini-2.5-flash"

    def test_custom_model_override(self) -> None:
        provider = GeminiProvider(api_key="test", model="custom-model")
        assert provider._model == "custom-model"


# ---------------------------------------------------------------------------
# get_provider factory tests
# ---------------------------------------------------------------------------


class TestGetProvider:
    """get_provider() wires the correct provider from config."""

    def test_default_is_groq_no_fallback(self) -> None:
        with patch.dict("os.environ", {"AI_PROVIDER": "groq", "GROQ_API_KEY": "test"}):
            provider = get_provider()
            # Without fallback: RetryableProvider wrapping GroqProvider
            assert isinstance(provider, RetryableProvider)
            assert isinstance(provider._inner, GroqProvider)

    def test_with_fallback_creates_chain(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AI_PROVIDER": "groq",
                "GROQ_API_KEY": "test",
                "AI_FALLBACK_PROVIDER": "gemini",
                "GEMINI_API_KEY": "test",
            },
        ):
            provider = get_provider()
            # With fallback: FallbackChain wrapping two RetryableProviders
            assert isinstance(provider, FallbackChain)
            assert isinstance(provider._primary, RetryableProvider)
            assert isinstance(provider._primary._inner, GroqProvider)
            assert isinstance(provider._fallback, RetryableProvider)
            assert isinstance(provider._fallback._inner, GeminiProvider)

    def test_explicit_provider_name(self) -> None:
        with patch.dict("os.environ", {}):
            provider = get_provider("groq", api_key="test-key")
            assert isinstance(provider, RetryableProvider)
            assert isinstance(provider._inner, GroqProvider)

    def test_unknown_provider_raises(self) -> None:
        with patch.dict("os.environ", {}):
            with pytest.raises(ValueError, match="Unknown AI provider"):
                get_provider("nonexistent")

    def test_register_provider(self) -> None:
        """Third-party providers can register themselves."""

        class CustomProvider:
            async def complete_json(self, *, system: str, user: str) -> dict:
                return {}

        register_provider("custom", CustomProvider)  # type: ignore[arg-type]
        with patch.dict("os.environ", {}):
            provider = get_provider("custom")
            assert isinstance(provider, RetryableProvider)
            assert isinstance(provider._inner, CustomProvider)

        # Clean up
        from track_a.ai_provider import _PROVIDERS

        del _PROVIDERS["custom"]


# ---------------------------------------------------------------------------
# Provider swap test (OCP compliance)
# ---------------------------------------------------------------------------


class TestProviderSwap:
    """Proving the pipeline works with ANY conforming AIProvider."""

    def test_pipeline_works_with_fake_provider(self) -> None:
        """The IntentParser works identically with a non-Groq provider."""
        script = {
            "change my hours to 9-6": {
                "action": "update",
                "content_type": "business_info",
                "fields": {"hours": "9-6"},
                "confidence": 0.95,
            }
        }
        provider = FakeProvider(script)
        parser = IntentParser(llm=provider)
        result = _run_parse(parser, "change my hours to 9-6")
        assert result.status == "intent"
        assert result.intent is not None
        assert result.intent["content_type"] == "business_info"
        assert result.intent["fields"]["hours"] == "9-6"

    def test_pipeline_works_with_exploding_provider(self) -> None:
        """Pipeline degrades gracefully with a failing provider."""
        parser = IntentParser(llm=ExplodingProvider())
        result = _run_parse(parser, "change my hours to 9-6")
        assert result.status == "low_confidence"
        assert result.intent is None

    def test_provider_protocol_compliance(self) -> None:
        """Any class with complete_json is substitutable."""

        class MinimalProvider:
            async def complete_json(self, *, system: str, user: str) -> dict:
                return {"unsupported": True}

        provider = MinimalProvider()
        parser = IntentParser(llm=provider)
        result = _run_parse(parser, "redesign my homepage")
        assert result.status == "unsupported"


# ---------------------------------------------------------------------------
# Live Groq integration test (optional — runs against real API)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("os").environ.get("GROQ_API_KEY"),
    reason="GROQ_API_KEY not set; skipping live integration test",
)
class TestGroqLive:
    """Live test against Groq's free-tier API.

    Run with: GROQ_API_KEY=... pytest track-a/tests/test_ai_provider.py -v -k TestGroqLive
    """

    def test_live_groq_returns_valid_intent(self) -> None:
        """Groq returns a valid intent for a clear message."""
        provider = GroqProvider()
        parser = IntentParser(llm=provider)
        result = _run_parse(parser, "change my hours to 9-6")
        assert result.status == "intent"
        assert result.intent is not None
        assert result.intent["content_type"] == "business_info"
        assert "hours" in result.intent["fields"]
        assert 0.0 <= result.confidence <= 1.0

    def test_live_groq_handles_unsupported(self) -> None:
        """Groq correctly identifies out-of-scope requests."""
        provider = GroqProvider()
        parser = IntentParser(llm=provider)
        result = _run_parse(parser, "redesign my homepage")
        assert result.status == "unsupported"
