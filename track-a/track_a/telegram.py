"""Telegram Bot API adapter for testing without WhatsApp.

Thin bridge that maps Telegram's ``chat_id`` to the router's ``owner_id``
and routes every incoming text message through the same
``IntentRouter.handle_message`` pipeline the WhatsApp webhook uses.

Two receive modes:
- **Webhook**: ``POST /telegram/webhook`` — set this URL in BotFather's
  ``setWebhook`` call.  Telegram delivers updates here.
- **Polling** (dev only): ``GET /telegram/poll`` — long-polls Telegram for
  updates.  Useful for local testing without a public URL.

The reply sender always goes through the Telegram Bot API ``sendMessage``
endpoint, so replies appear in the same Telegram chat the owner used.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("track_a.telegram")


class TelegramReplySender:
    """Sends text messages via the Telegram Bot API.

    Same ``send(owner_id, text)`` interface as ``WhatsAppReplySender``
    so the router can use either interchangeably.
    """

    def __init__(self, bot_token: str, client: httpx.AsyncClient | None = None) -> None:
        self.bot_token = bot_token
        self._client = client or httpx.AsyncClient()

    async def send(self, to: str, text: str) -> None:
        if not self.bot_token:
            logger.warning("TelegramReplySender not configured; logging reply to %s: %s", to, text)
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        # Strip the owner_prefix ("tg_") that handle_telegram_update adds,
        # because Telegram's API needs the raw numeric chat_id.
        chat_id = to.removeprefix("tg_")
        payload = {"chat_id": chat_id, "text": text}
        try:
            resp = await self._client.post(url, json=payload, timeout=15.0)
            resp.raise_for_status()
            logger.info("sent Telegram message to %s (status %s)", to, resp.status_code)
        except httpx.HTTPStatusError as exc:
            logger.error("Telegram send failed for %s: %s", to, exc)
        except Exception as exc:
            logger.error("Telegram send error for %s: %s", to, exc)


async def handle_telegram_update(
    update: dict[str, Any],
    *,
    router: Any,
    owner_prefix: str = "tg_",
) -> dict[str, str]:
    """Process one Telegram update dict through the conversation pipeline.

    Returns ``{"status": "ok"}`` on success.  Errors are logged but never
    raised — the caller must always answer Telegram with HTTP 200.
    """
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"status": "ok"}  # non-text update (callback, etc.)

    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    text = message.get("text", "")

    if not chat_id or not text:
        return {"status": "ok"}

    # Map Telegram chat_id to an owner_id the router understands.
    owner_id = f"{owner_prefix}{chat_id}"

    try:
        await router.handle_message(owner_id, text)
    except Exception:
        logger.exception("router failed for Telegram chat %s", chat_id)

    return {"status": "ok"}
