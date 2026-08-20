"""Outbound WhatsApp replies.

`WhatsAppReplySender` is the real mechanism: POST
/{{version}}/{{phone_number_id}}/messages on the Graph API with the
system-user access token. `ReplySender` is the dev/default sender that
only logs (used when no token/number is configured, and by tests).

The voice-note fallback text lives here too (used by the pipeline): when
a voice note cannot be trusted — empty/garbled transcript, low
confidence, or Whisper detecting no speech — do NOT proceed to intent
parsing, and instead ask the owner to resend as text or try again.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("track_a.reply")

FALLBACK_REPLY_TEXT = (
    "Sorry — I couldn't make out your voice message. "
    "Could you try sending it again, or type your request as a text message?"
)


class ReplySender:
    """Dev/default sender: logs the intended reply, sends nothing."""

    async def send(self, to: str, text: str) -> None:
        logger.info("[dev] would reply to %s: %s", to, text)


class WhatsAppReplySender:
    """Real sender: POST /{version}/{phone_number_id}/messages (Graph API).

    Falls back to logging when no api_token / phone_number_id is
    configured, so local dev still works without Meta credentials.
    """

    def __init__(
        self,
        api_token: str = "",
        phone_number_id: str = "",
        api_version: str = "v21.0",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_token = api_token
        self.phone_number_id = phone_number_id
        self.api_version = api_version
        self._client = client

    async def send(self, to: str, text: str) -> None:
        if not self.api_token or not self.phone_number_id:
            logger.warning(
                "WhatsAppReplySender not configured; logging reply to %s: %s",
                to,
                text,
            )
            return

        url = f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }
        client = self._client or httpx.AsyncClient()
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=30.0)
            resp.raise_for_status()
            logger.info("sent WhatsApp message to %s (status %s)", to, resp.status_code)
        finally:
            if self._client is None:
                await client.aclose()
