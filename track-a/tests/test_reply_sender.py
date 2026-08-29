"""WhatsAppReplySender: the real outbound mechanism (Graph API)."""

import asyncio

import httpx

from track_a.reply import WhatsAppReplySender

OWNER = "15551234567"


def run(coro):
    return asyncio.run(coro)


def test_send_posts_to_graph_api_with_auth_and_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["payload"] = request.read().decode()
        return httpx.Response(200, json={"messages": [{"id": "wamid-1"}]})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    sender = WhatsAppReplySender(
        api_token="secret-token",
        phone_number_id="123456789",
        api_version="v21.0",
        client=http,
    )

    run(sender.send(OWNER, "Reply YES to publish, NO to cancel."))

    assert captured["url"] == "https://graph.facebook.com/v21.0/123456789/messages"
    # httpx normalizes header names to lowercase on read.
    assert captured["headers"].get("authorization") == "Bearer secret-token"
    assert captured["headers"].get("content-type") == "application/json"
    import json

    payload = json.loads(captured["payload"])
    assert payload == {
        "messaging_product": "whatsapp",
        "to": OWNER,
        "type": "text",
        "text": {"body": "Reply YES to publish, NO to cancel."},
    }


def test_send_without_credentials_logs_and_sends_nothing():
    """Dev mode: no token/number configured -> no HTTP call, no crash."""
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    sender = WhatsAppReplySender(client=http)

    run(sender.send(OWNER, "hello"))
    assert called == []


def test_send_http_error_does_not_raise():
    """Graph API errors are logged, not raised — caller never crashes."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limit"}})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    sender = WhatsAppReplySender(
        api_token="token",
        phone_number_id="123",
        client=http,
    )
    # Should not raise
    run(sender.send(OWNER, "hello"))


def test_send_connection_error_does_not_raise():
    """Network errors are logged, not raised."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    sender = WhatsAppReplySender(
        api_token="token",
        phone_number_id="123",
        client=http,
    )
    # Should not raise
    run(sender.send(OWNER, "hello"))
