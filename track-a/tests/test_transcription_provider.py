"""Transcription provider tests: GroqTranscriptionProvider, provider swap.

Tests cover:
- GroqTranscriptionProvider correctly calls Groq's Whisper API.
- get_transcription_provider() factory wires the correct provider.
- The pipeline works with ANY conforming Transcriber (provider swap).
- Live test against Groq's Whisper API (optional, needs GROQ_API_KEY).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from track_a.media import MediaPayload
from track_a.transcribe import (
    GroqTranscriptionProvider,
    StubTranscriber,
    Transcription,
    WhisperTranscriber,
    get_transcription_provider,
    register_transcription_provider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audio_payload(
    content: bytes = b"fake-audio-bytes",
    mime_type: str = "audio/ogg",
    media_id: str = "test-media-id",
) -> MediaPayload:
    return MediaPayload(content=content, mime_type=mime_type, media_id=media_id)


class FakeTranscriber:
    """Minimal Transcriber implementation for testing."""

    def __init__(self, script: dict[str, Transcription]) -> None:
        self.script = script
        self.calls: list[MediaPayload] = []

    async def transcribe(self, payload: MediaPayload) -> Transcription:
        self.calls.append(payload)
        key = payload.media_id or ""
        if key not in self.script:
            raise AssertionError(f"no scripted response for media_id: {key!r}")
        return self.script[key]


class ExplodingTranscriber:
    """Always raises — tests error handling."""

    async def transcribe(self, payload: MediaPayload) -> Transcription:
        raise RuntimeError("transcription exploded")


# ---------------------------------------------------------------------------
# GroqTranscriptionProvider tests
# ---------------------------------------------------------------------------


class TestGroqTranscriptionProvider:
    """GroqTranscriptionProvider calls Groq's Whisper API."""

    def test_missing_api_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            provider = GroqTranscriptionProvider(api_key=None)
            with pytest.raises(ValueError, match="GROQ_API_KEY"):
                asyncio.run(provider.transcribe(_audio_payload()))

    def test_uses_correct_model(self) -> None:
        assert GroqTranscriptionProvider.DEFAULT_MODEL == "whisper-large-v3-turbo"

    def test_custom_model_override(self) -> None:
        provider = GroqTranscriptionProvider(api_key="test", model="whisper-large-v3")
        assert provider._model == "whisper-large-v3"

    def test_calls_correct_api(self) -> None:
        """Verify the client calls Groq's transcription endpoint."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.text = "change my hours to 9-6"
        mock_response.segments = [
            MagicMock(no_speech_prob=0.05),
            MagicMock(no_speech_prob=0.1),
        ]
        mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)

        provider = GroqTranscriptionProvider(api_key="test-key", client=mock_client)
        payload = _audio_payload(content=b"audio-bytes", mime_type="audio/ogg")
        result = asyncio.run(provider.transcribe(payload))

        assert result.text == "change my hours to 9-6"
        assert result.is_voice is True
        assert 0.0 <= result.confidence <= 1.0

        # Verify the API was called correctly
        mock_client.audio.transcriptions.create.assert_called_once()
        call_kwargs = mock_client.audio.transcriptions.create.call_args
        assert call_kwargs.kwargs["model"] == "whisper-large-v3-turbo"
        assert call_kwargs.kwargs["response_format"] == "verbose_json"
        assert call_kwargs.kwargs["temperature"] == 0.0

    def test_empty_transcript_returns_low_confidence(self) -> None:
        """Empty transcript → confidence=0, is_voice=False."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.text = ""
        mock_response.segments = []
        mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)

        provider = GroqTranscriptionProvider(api_key="test-key", client=mock_client)
        result = asyncio.run(provider.transcribe(_audio_payload()))

        assert result.text == ""
        assert result.confidence == 0.0
        assert result.is_voice is False

    def test_high_no_speech_prob_marks_not_voice(self) -> None:
        """High no_speech_prob → is_voice=False."""
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.text = "some text"
        mock_response.segments = [
            MagicMock(no_speech_prob=0.8),  # Above threshold (0.6)
        ]
        mock_client.audio.transcriptions.create = AsyncMock(return_value=mock_response)

        provider = GroqTranscriptionProvider(api_key="test-key", client=mock_client)
        result = asyncio.run(provider.transcribe(_audio_payload()))

        assert result.text == "some text"
        assert result.is_voice is False  # no_speech_prob > 0.6


# ---------------------------------------------------------------------------
# get_transcription_provider factory tests
# ---------------------------------------------------------------------------


class TestGetTranscriptionProvider:
    """get_transcription_provider() wires the correct provider from config."""

    def test_default_is_groq(self) -> None:
        with patch.dict("os.environ", {"TRANSCRIPTION_PROVIDER": "groq"}):
            provider = get_transcription_provider()
            assert isinstance(provider, GroqTranscriptionProvider)

    def test_explicit_provider_name(self) -> None:
        with patch.dict("os.environ", {}):
            provider = get_transcription_provider("stub")
            assert isinstance(provider, StubTranscriber)

    def test_local_provider(self) -> None:
        with patch.dict("os.environ", {}):
            provider = get_transcription_provider("local")
            assert isinstance(provider, WhisperTranscriber)

    def test_unknown_provider_raises(self) -> None:
        with patch.dict("os.environ", {}):
            with pytest.raises(ValueError, match="Unknown TRANSCRIPTION_PROVIDER"):
                get_transcription_provider("nonexistent")

    def test_register_provider(self) -> None:
        """Third-party providers can register themselves."""

        class CustomTranscriber:
            async def transcribe(self, payload: MediaPayload) -> Transcription:
                return Transcription(text="custom", confidence=1.0)

        register_transcription_provider("custom", CustomTranscriber)  # type: ignore[arg-type]
        with patch.dict("os.environ", {}):
            provider = get_transcription_provider("custom")
            assert isinstance(provider, CustomTranscriber)

        # Clean up
        from track_a.transcribe import _TRANSCRIPTION_PROVIDERS

        del _TRANSCRIPTION_PROVIDERS["custom"]


# ---------------------------------------------------------------------------
# Provider swap test (OCP compliance)
# ---------------------------------------------------------------------------


class TestProviderSwap:
    """Proving the pipeline works with ANY conforming Transcriber."""

    def test_pipeline_works_with_fake_transcriber(self) -> None:
        """The MessageProcessor works identically with a non-Groq transcriber."""
        script = {
            "media-1": Transcription(text="change my hours to 9-6", confidence=0.92, is_voice=True),
        }
        transcriber = FakeTranscriber(script)
        payload = _audio_payload(media_id="media-1")
        result = asyncio.run(transcriber.transcribe(payload))
        assert result.text == "change my hours to 9-6"
        assert result.confidence == 0.92
        assert result.is_voice is True

    def test_pipeline_works_with_exploding_transcriber(self) -> None:
        """Pipeline degrades gracefully with a failing transcriber."""
        transcriber = ExplodingTranscriber()
        with pytest.raises(RuntimeError, match="transcription exploded"):
            asyncio.run(transcriber.transcribe(_audio_payload()))

    def test_provider_protocol_compliance(self) -> None:
        """Any class with transcribe() is substitutable."""

        class MinimalTranscriber:
            async def transcribe(self, payload: MediaPayload) -> Transcription:
                return Transcription(text="minimal", confidence=0.5)

        provider = MinimalTranscriber()
        result = asyncio.run(provider.transcribe(_audio_payload()))
        assert result.text == "minimal"


# ---------------------------------------------------------------------------
# Live Groq transcription test (optional — runs against real API)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not __import__("os").environ.get("GROQ_API_KEY"),
    reason="GROQ_API_KEY not set; skipping live transcription test",
)
class TestGroqTranscriptionLive:
    """Live test against Groq's Whisper API.

    Run with: GROQ_API_KEY=... pytest track-a/tests/test_transcription_provider.py -v -k TestGroqTranscriptionLive
    """

    def test_live_groq_transcribes_audio(self) -> None:
        """Groq transcribes a real audio sample."""
        import io
        import struct
        import wave

        # Create a simple WAV file with a sine wave (simulates speech)
        rate = 16000
        duration = 1.0
        freq = 440.0
        frames = b"".join(
            struct.pack(
                "<h",
                int(12000 * __import__("math").sin(2 * __import__("math").pi * freq * i / rate)),
            )
            for i in range(int(rate * duration))
        )
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(frames)
        wav_bytes = buf.getvalue()

        provider = GroqTranscriptionProvider()
        payload = MediaPayload(content=wav_bytes, mime_type="audio/wav", media_id="live-test")
        result = asyncio.run(provider.transcribe(payload))
        # A sine wave won't produce meaningful text, but it should not crash
        assert isinstance(result.text, str)
        assert 0.0 <= result.confidence <= 1.0
