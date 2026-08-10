"""WhatsApp Cloud API media download.

Two-step flow per Meta's Media API:
1. GET /{graph_version}/{media_id}  -> {"url": ..., "mime_type": ...}
2. GET {url}                        -> raw media bytes

Both calls require the system-user access token as a Bearer header.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

GRAPH_API_BASE = "https://graph.facebook.com"


@dataclass
class MediaPayload:
    """Downloaded media bytes plus the context we need downstream."""

    content: bytes
    mime_type: str
    media_id: str | None = None


class WhatsAppMediaClient:
    def __init__(
        self,
        api_token: str,
        api_version: str = "v21.0",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_token = api_token
        self.api_version = api_version
        self._client = client or httpx.AsyncClient()

    async def download_media(self, media_id: str) -> MediaPayload:
        """Download a media object (e.g. a voice note) by its media id."""
        if not self.api_token:
            raise ValueError(
                "WHATSAPP_API_TOKEN is not configured; cannot download media"
            )
        headers = {"Authorization": f"Bearer {self.api_token}"}

        info_resp = await self._client.get(
            f"{GRAPH_API_BASE}/{self.api_version}/{media_id}",
            headers=headers,
            timeout=30.0,
        )
        info_resp.raise_for_status()
        info = info_resp.json()

        url = info.get("url")
        if not url:
            raise ValueError(f"media info for {media_id!r} has no download url")

        media_resp = await self._client.get(url, headers=headers, timeout=60.0)
        media_resp.raise_for_status()

        mime_type = media_resp.headers.get(
            "content-type", info.get("mime_type", "audio/ogg")
        )
        return MediaPayload(
            content=media_resp.content,
            mime_type=mime_type,
            media_id=media_id,
        )
