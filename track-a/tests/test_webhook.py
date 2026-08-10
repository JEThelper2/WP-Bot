"""Tests for the Meta Cloud API webhook receiver (handshake + logging)."""

import pytest
from fastapi.testclient import TestClient

from track_a.config import Settings
from track_a.main import create_app
from track_a.store import count_messages, list_messages

VERIFY_TOKEN = "test-verify-token"


@pytest.fixture()
def app(tmp_path):
    settings = Settings(
        verify_token=VERIFY_TOKEN,
        track_b_url="http://track-b:8200",
        db_path=tmp_path / "inbound.db",
    )
    return create_app(settings)


@pytest.fixture()
def client(app):
    return TestClient(app)


def db_path(app):
    return app.state.settings.db_path


def sample_text_payload(body: str = "change my hours to 9-6") -> dict:
    """Meta's documented sample webhook payload shape (text message)."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [
                                {"profile": {"name": "Test User"}, "wa_id": "15551234567"}
                            ],
                            "messages": [
                                {
                                    "from": "15551234567",
                                    "id": "wamid.HBgNNDU1NTEyMzQ1NjcVAgASGBQ5QkVCOEEtQTVBQi03QkItNDhCQSA=",
                                    "timestamp": "1700000000",
                                    "text": {"body": body},
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def sample_audio_payload() -> dict:
    """Voice-note message (Meta sends audio with `voice: true`)."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [
                                {"profile": {"name": "Test User"}, "wa_id": "15551234567"}
                            ],
                            "messages": [
                                {
                                    "from": "15551234567",
                                    "id": "wamid.HBgNQVVESU8tMTIzNDU2Nzg5VAg",
                                    "timestamp": "1700000001",
                                    "type": "audio",
                                    "audio": {
                                        "mime_type": "audio/ogg; codecs=opus",
                                        "sha256": "abc123",
                                        "id": "1234567890123456",
                                        "voice": True,
                                    },
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------- handshake


def test_verification_handshake_returns_challenge(client: TestClient) -> None:
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "1158201444"


def test_verification_handshake_rejects_wrong_token(client: TestClient) -> None:
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "1158201444",
        },
    )
    assert resp.status_code == 403


def test_verification_handshake_rejects_wrong_mode(client: TestClient) -> None:
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert resp.status_code == 403


# ------------------------------------------------------------------ receive


def test_text_message_is_received_and_logged(client: TestClient, app) -> None:
    resp = client.post("/webhook", json=sample_text_payload())
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "received": 1, "duplicates": 0}

    rows = list_messages(db_path(app))
    assert len(rows) == 1
    row = rows[0]
    assert row["owner_phone"] == "15551234567"
    assert row["message_type"] == "text"
    assert row["content"] == "change my hours to 9-6"
    assert row["media_ref"] is None
    assert row["meta_timestamp"] == "1700000000"
    assert row["wam_id"].startswith("wamid.")


def test_voice_note_is_received_and_logged_with_media_ref(client: TestClient, app) -> None:
    resp = client.post("/webhook", json=sample_audio_payload())
    assert resp.status_code == 200
    assert resp.json()["received"] == 1

    rows = list_messages(db_path(app))
    assert len(rows) == 1
    row = rows[0]
    assert row["owner_phone"] == "15551234567"
    assert row["message_type"] == "audio"
    assert row["media_ref"] == "1234567890123456"
    assert "audio/ogg" in row["content"]
    assert '"voice":true' in row["content"]


def test_duplicate_delivery_is_not_double_logged(client: TestClient, app) -> None:
    payload = sample_text_payload()
    assert client.post("/webhook", json=payload).json()["received"] == 1
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "received": 0, "duplicates": 1}
    assert count_messages(db_path(app)) == 1


def test_multiple_messages_in_one_delivery_are_all_logged(client: TestClient, app) -> None:
    payload = sample_text_payload()
    audio = sample_audio_payload()["entry"][0]["changes"][0]["value"]["messages"][0]
    payload["entry"][0]["changes"][0]["value"]["messages"].append(audio)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json()["received"] == 2
    assert count_messages(db_path(app)) == 2


def test_delivery_receipt_statuses_are_ignored_but_answered_200(
    client: TestClient, app
) -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "statuses": [
                                {
                                    "id": "wamid.ABGG",
                                    "status": "sent",
                                    "timestamp": "1700000000",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json()["received"] == 0
    assert count_messages(db_path(app)) == 0


def test_unknown_object_gets_404(client: TestClient) -> None:
    resp = client.post("/webhook", json={"object": "something_else", "entry": []})
    assert resp.status_code == 404


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
