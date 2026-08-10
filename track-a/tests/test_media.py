"""Tests for the WhatsApp Media API client."""

import asyncio

import httpx
import pytest

from track_a.media import WhatsAppMediaClient

TOKEN = "test-access-token"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if url == "https://graph.facebook.com/v21.0/MEDIA_123":
            return httpx.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/audio/MEDIA_123.ogg",
                    "mime_type": "audio/ogg; codecs=opus",
                    "id": "MEDIA_123",
                },
            )
        if url == "https://lookaside.fbsbx.com/audio/MEDIA_123.ogg":
            return httpx.Response(
                200,
                content=b"\x4f\x67\x67Sfake-audio-bytes",
                headers={"content-type": "audio/ogg; codecs=opus"},
            )
        raise AssertionError(f"unexpected request: {url}")

    return httpx.MockTransport(handler)


def test_download_media_two_step_with_bearer_auth(mock_transport) -> None:
    http = httpx.AsyncClient(transport=mock_transport)
    client = WhatsAppMediaClient(api_token=TOKEN, api_version="v21.0", client=http)

    payload = run(client.download_media("MEDIA_123"))
    assert payload.content == b"\x4f\x67\x67Sfake-audio-bytes"
    assert payload.mime_type == "audio/ogg; codecs=opus"
    assert payload.media_id == "MEDIA_123"


def test_missing_token_raises_before_any_request() -> None:
    client = WhatsAppMediaClient(api_token="", client=httpx.AsyncClient())
    with pytest.raises(ValueError, match="WHATSAPP_API_TOKEN"):
        run(client.download_media("MEDIA_123"))


def test_media_info_error_propagates(mock_transport) -> None:
    def failing_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://graph.facebook.com"):
            return httpx.Response(400, json={"error": {"message": "bad id"}})
        raise AssertionError("should not reach the download step")

    http = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))
    client = WhatsAppMediaClient(api_token=TOKEN, client=http)
    with pytest.raises(httpx.HTTPStatusError):
        run(client.download_media("BOGUS"))
