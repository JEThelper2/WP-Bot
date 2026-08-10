"""Inbound message pipeline (voice-note milestone).

Turns raw inbound messages into a single, channel-agnostic
`message_text` that the next step (intent parsing) can consume without
caring whether the owner typed or spoke:

- text messages  -> message_text = the raw body
- voice notes    -> download -> transcribe -> message_text = transcript
- anything else  -> unsupported, no message_text

**Fallback rule:** if a voice note's transcript is empty/garbled, the
transcription confidence is below the threshold, or Whisper detected no
speech, the message does NOT get a message_text (so intent parsing will
skip it) and a reply is prepared asking the owner to resend as text or
try again. The send mechanism itself is stubbed (`reply.ReplySender`);
A5 wires the real Graph API send.

The pipeline is synchronous within the webhook request for now; when
transcription gets slow in production this should move to a queue/worker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import MediaPayload, WhatsAppMediaClient
from .reply import FALLBACK_REPLY_TEXT, ReplySender
from .store import get_message, update_processing
from .transcribe import Transcriber

logger = logging.getLogger("track_a.pipeline")

# Content types that never produce message_text (image/video/etc.).
# Only "audio" (voice notes) is transcribed.
_TRANSCRIBED_TYPE = "audio"


@dataclass
class ProcessingOutcome:
    status: str  # text | transcribed | low_confidence | failed | unsupported
    message_text: str | None = None
    reply_text: str | None = None


class MessageProcessor:
    def __init__(
        self,
        db_path: Path,
        media_client: WhatsAppMediaClient | Any,
        transcriber: Transcriber | Any,
        sender: ReplySender | Any,
        confidence_threshold: float = 0.5,
    ) -> None:
        self.db_path = db_path
        self.media_client = media_client
        self.transcriber = transcriber
        self.sender = sender
        self.confidence_threshold = confidence_threshold

    async def process_row(self, row_id: int) -> ProcessingOutcome:
        """Process one logged inbound message; persist status + message_text."""
        row = get_message(self.db_path, row_id)
        if row is None:
            raise ValueError(f"no inbound message row with id {row_id}")

        message_type = row["message_type"]
        if message_type == "text":
            outcome = ProcessingOutcome(
                status="text",
                message_text=(row["content"] or "").strip(),
            )
        elif message_type == _TRANSCRIBED_TYPE:
            outcome = await self._process_voice(row)
        else:
            # image/video/sticker/document/unknown: logged but not parseable
            outcome = ProcessingOutcome(status="unsupported")

        update_processing(
            self.db_path,
            row_id,
            status=outcome.status,
            message_text=outcome.message_text,
        )

        if outcome.reply_text is not None:
            await self.sender.send(row["owner_phone"], outcome.reply_text)

        logger.info("message %s -> status=%s", row_id, outcome.status)
        return outcome

    async def _process_voice(self, row: dict) -> ProcessingOutcome:
        media_ref = row["media_ref"]
        try:
            payload: MediaPayload = await self.media_client.download_media(media_ref)
            transcription = await self.transcriber.transcribe(payload)
        except Exception as exc:  # download or transcription failure
            logger.warning(
                "voice processing failed for message %s (%s): %s",
                row["id"],
                media_ref,
                exc,
            )
            return ProcessingOutcome(status="failed", reply_text=FALLBACK_REPLY_TEXT)

        text = transcription.text.strip()
        low_confidence = (
            not transcription.is_voice
            or transcription.confidence < self.confidence_threshold
            or not text
        )
        if low_confidence:
            logger.info(
                "voice note %s routed to fallback: text=%r confidence=%.2f is_voice=%s",
                row["id"],
                text[:80],
                transcription.confidence,
                transcription.is_voice,
            )
            return ProcessingOutcome(
                status="low_confidence", reply_text=FALLBACK_REPLY_TEXT
            )

        return ProcessingOutcome(status="transcribed", message_text=text)
