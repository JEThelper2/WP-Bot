"""Outbound WhatsApp replies.

A5 builds the real "send message" mechanism (POST /{phone_number_id}/messages
on the Graph API). Until then, `ReplySender` is a stub that only logs —
but the *conditions* and *message text* used for the voice-note fallback
live here, so the routing logic is final.

Routing rule (used by the pipeline): when a voice note cannot be trusted —
empty/garbled transcript, low transcription confidence, or Whisper
detecting no speech — do NOT proceed to intent parsing. Instead reply
asking the owner to resend as text or try the voice note again.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("track_a.reply")

FALLBACK_REPLY_TEXT = (
    "Sorry — I couldn't make out your voice message. "
    "Could you try sending it again, or type your request as a text message?"
)


class ReplySender:
    """Placeholder sender. A5 replaces this with the real Graph API call."""

    async def send(self, to: str, text: str) -> None:
        # A5: POST https://graph.facebook.com/{version}/{phone_number_id}/messages
        # with the access token; for now we only log the intended reply.
        logger.info("[A5 stub] would reply to %s: %s", to, text)
