"""Voice transcription.

The pipeline talks to a `Transcriber` — an audio payload in, a
`Transcription` (text + confidence + speech-detected flag) out. The real
implementation is Whisper via `faster-whisper` (PyAV-based, so it does
not need an ffmpeg binary). It is an optional dependency
(`pip install -e ./track-a[transcribe]`) and is imported lazily so the
rest of Track A works without it.

Confidence model (mirrors how Whisper exposes its own estimates):
- `avg_logprob` per segment is the mean log-probability of the decoded
  tokens; we convert it with exp() into a 0..1 score.
- `no_speech_prob` per segment is Whisper's estimate that a segment
  contains no speech; the max across segments drives `is_voice`.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
from dataclasses import dataclass
from typing import Protocol

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


class WhisperTranscriber:
    """Whisper transcription via faster-whisper (optional dependency)."""

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
