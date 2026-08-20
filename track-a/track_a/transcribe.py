"""Voice transcription.

The pipeline talks to a `Transcriber` — an audio payload in, a
`Transcription` (text + confidence + speech-detected flag) out.

Provider selection (Dependency Inversion):
    TRANSCRIPTION_PROVIDER — provider name (e.g. "groq").  Default: "groq".
    Reuses the same GROQ_API_KEY as the intent-parsing layer.

Implementations:
- `GroqTranscriptionProvider` — Groq's hosted Whisper (default, free tier).
- `WhisperTranscriber` — local Whisper via `faster-whisper` (optional dep).
- `StubTranscriber` — scripted fake for tests.

Adding a new provider means: one class implementing `Transcriber` +
one branch in `get_transcription_provider()`.  No pipeline changes.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Protocol

from .media import MediaPayload

logger = logging.getLogger("track_a.transcribe")

# Whisper's per-segment no-speech probability above which we treat the
# clip as not containing speech.
NO_SPEECH_THRESHOLD = 0.6


@dataclass
class Transcription:
    text: str
    confidence: float  # 0.0 - 1.0
    is_voice: bool = True  # False when Whisper detected no speech


class Transcriber(Protocol):
    async def transcribe(self, payload: MediaPayload) -> Transcription: ...


class GroqTranscriptionProvider:
    """Groq's hosted Whisper API for speech-to-text.

    Uses the same GROQ_API_KEY as the intent-parsing layer — one account,
    one key, no second service to manage.  The API is OpenAI-compatible
    (same endpoint shape), so we reuse the `openai` SDK.

    Env vars:
        GROQ_API_KEY     — required (shared with GroqProvider for intents)
        GROQ_WHISPER_MODEL — optional, defaults to ``whisper-large-v3-turbo``
    """

    # whisper-large-v3-turbo: fast, cheap ($0.04/hr), 12% WER.
    # whisper-large-v3: more accurate (10.3% WER) but slower/costlier.
    # Turbo is the right default for a real-time WhatsApp bot.
    DEFAULT_MODEL = "whisper-large-v3-turbo"
    API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        client: Any = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("GROQ_API_KEY")
        self._model = model or os.environ.get(
            "GROQ_WHISPER_MODEL", self.DEFAULT_MODEL
        )
        self._client = client  # injected for testing

    async def transcribe(self, payload: MediaPayload) -> Transcription:
        if not self._api_key:
            raise ValueError(
                "GROQ_API_KEY is not configured; cannot transcribe audio"
            )

        from openai import AsyncOpenAI

        client = self._client or AsyncOpenAI(
            api_key=self._api_key,
            base_url="https://api.groq.com/openai/v1",
        )

        # The openai SDK expects a file-like object.
        file_obj = io.BytesIO(payload.content)
        file_obj.name = _extension_for_mime(payload.mime_type)

        response = await client.audio.transcriptions.create(
            model=self._model,
            file=file_obj,
            response_format="verbose_json",
            temperature=0.0,
        )

        text = (response.text or "").strip()
        # Groq's verbose_json returns segments with no_speech_prob.
        # Extract the max no_speech_prob across segments for is_voice.
        segments = getattr(response, "segments", []) or []
        no_speech_probs = [
            getattr(s, "no_speech_prob", 0.0) for s in segments
        ]
        no_speech = max(no_speech_probs) if no_speech_probs else 0.0

        # Groq doesn't expose avg_logprob directly in verbose_json;
        # use 1.0 - no_speech as a proxy for confidence when text is present,
        # or 0.0 when no text.
        if text:
            confidence = max(0.0, min(1.0, 1.0 - no_speech))
        else:
            confidence = 0.0

        is_voice = no_speech < NO_SPEECH_THRESHOLD and bool(text)
        logger.debug(
            "groq transcription: %r confidence=%.2f no_speech=%.2f",
            text[:80],
            confidence,
            no_speech,
        )
        return Transcription(text=text, confidence=confidence, is_voice=is_voice)


def _extension_for_mime(mime_type: str) -> str:
    """Map a MIME type to a file extension for the OpenAI SDK."""
    mapping = {
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/mp3": ".mp3",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".mp4",
        "audio/m4a": ".m4a",
        "audio/flac": ".flac",
        "audio/webm": ".webm",
    }
    return mapping.get(mime_type, ".ogg")  # WhatsApp voice notes are OGG


class WhisperTranscriber:
    """Whisper transcription via faster-whisper (optional local dependency).

    This runs Whisper locally on-device.  Prefer GroqTranscriptionProvider
    for production (faster, no model download, free tier).
    """

    def __init__(
        self,
        model_size: str = "tiny",
        device: str = "cpu",
        compute_type: str = "int8",
        vad_filter: bool = False,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        # VAD (Silero via onnxruntime) trims non-speech before decoding, but
        # onnxruntime fails to load on some Windows hosts; Whisper's own
        # per-segment no_speech_prob drives is_voice either way, so VAD is
        # an optional optimization rather than a requirement.
        self.vad_filter = vad_filter
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info(
                "loading faster-whisper model %s (%s/%s)",
                self.model_size,
                self.device,
                self.compute_type,
            )
            self._model = WhisperModel(
                self.model_size, device=self.device, compute_type=self.compute_type
            )
        return self._model

    async def transcribe(self, payload: MediaPayload) -> Transcription:
        model = await asyncio.to_thread(self._load)
        segments, info = await asyncio.to_thread(
            model.transcribe,
            io.BytesIO(payload.content),
            vad_filter=self.vad_filter,
        )

        texts: list[str] = []
        logprobs: list[float] = []
        no_speech_probs: list[float] = []
        for segment in segments:  # generator: consuming is blocking
            texts.append(segment.text.strip())
            logprobs.append(getattr(segment, "avg_logprob", 0.0))
            no_speech_probs.append(getattr(segment, "no_speech_prob", 0.0))

        text = " ".join(t for t in texts if t).strip()
        if logprobs:
            avg_logprob = sum(logprobs) / len(logprobs)
            confidence = max(0.0, min(1.0, math.exp(avg_logprob)))
        else:
            avg_logprob = 0.0
            confidence = 0.0
        no_speech = max(no_speech_probs) if no_speech_probs else 1.0

        is_voice = no_speech < NO_SPEECH_THRESHOLD and bool(text)
        logger.debug(
            "transcription: %r confidence=%.2f no_speech=%.2f avg_logprob=%.2f",
            text[:80],
            confidence,
            no_speech,
            avg_logprob,
        )
        return Transcription(text=text, confidence=confidence, is_voice=is_voice)


class StubTranscriber:
    """Scripted transcriber for development and tests.

    Returns a fixed `Transcription` per media id, so the routing logic
    can be exercised deterministically without a Whisper model.
    """

    def __init__(
        self,
        script: dict[str, Transcription] | None = None,
        default: Transcription | None = None,
    ) -> None:
        self.script = script or {}
        self.default = default or Transcription(text="", confidence=0.0, is_voice=False)

    async def transcribe(self, payload: MediaPayload) -> Transcription:
        return self.script.get(payload.media_id or "", self.default)


# ---------------------------------------------------------------------------
# Provider factory (startup wiring only)
# ---------------------------------------------------------------------------

# Registry of transcription providers keyed by the env value.
_TRANSCRIPTION_PROVIDERS: dict[str, type[Transcriber]] = {
    "groq": GroqTranscriptionProvider,
    "local": WhisperTranscriber,
    "stub": StubTranscriber,
}


def get_transcription_provider(
    name: str | None = None, **kwargs: Any
) -> Transcriber:
    """Instantiate the selected transcription provider.

    ``name`` defaults to the ``TRANSCRIPTION_PROVIDER`` env var, falling
    back to ``"groq"``.  Extra ``**kwargs`` are forwarded to the
    provider constructor.
    """
    name = (name or os.environ.get("TRANSCRIPTION_PROVIDER") or "groq").lower()
    cls = _TRANSCRIPTION_PROVIDERS.get(name)
    if cls is None:
        available = ", ".join(sorted(_TRANSCRIPTION_PROVIDERS))
        raise ValueError(
            f"Unknown TRANSCRIPTION_PROVIDER {name!r}; available: {available}"
        )
    return cls(**kwargs)


def register_transcription_provider(name: str, cls: type[Transcriber]) -> None:
    """Register a new transcription provider.

    Call from a third-party module to add a provider without modifying
    this file (Open/Closed Principle).
    """
    _TRANSCRIPTION_PROVIDERS[name] = cls
