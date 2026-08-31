"""Tests for the Meta Cloud API webhook receiver (handshake + logging)."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from track_a.config import Settings
from track_a.main import create_app
from track_a.store import count_messages, list_messages

VERIFY_TOKEN = "test-verify-token"
APP_SECRET = "test-app-secret"
ADMIN_TOKEN = "test-admin-token"


def sign_payload(secret: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class NoopProcessor:
    """Webhook tests focus on receive+log; skip the pipeline here."""

    async def process_row(self, row_id):
        return None


@pytest.fixture()
def app(tmp_path):
    settings = Settings(
        verify_token=VERIFY_TOKEN,
        track_b_url="http://track-b:8200",
        db_path=tmp_path / "inbound.db",
        admin_token=ADMIN_TOKEN,
    )
    return create_app(settings, processor=NoopProcessor())


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture()
def signed_app(tmp_path):
    """App with the app secret configured, so signatures are enforced."""
    settings = Settings(
        verify_token=VERIFY_TOKEN,
        track_b_url="http://track-b:8200",
        db_path=tmp_path / "inbound.db",
        app_secret=APP_SECRET,
    )
    return create_app(settings, processor=NoopProcessor())


@pytest.fixture()
def signed_client(signed_app):
    return TestClient(signed_app)


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
    data = resp.json()
    assert data == {"status": "ok", "received": 1, "duplicates": 0, "rate_limited": 0}

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
    assert resp.json() == {"status": "ok", "received": 0, "duplicates": 1, "rate_limited": 0}
    assert count_messages(db_path(app)) == 1


def test_multiple_messages_in_one_delivery_are_all_logged(client: TestClient, app) -> None:
    payload = sample_text_payload()
    audio = sample_audio_payload()["entry"][0]["changes"][0]["value"]["messages"][0]
    payload["entry"][0]["changes"][0]["value"]["messages"].append(audio)
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    assert resp.json()["received"] == 2
    assert count_messages(db_path(app)) == 2


def test_delivery_receipt_statuses_are_ignored_but_answered_200(client: TestClient, app) -> None:
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


# ------------------------------------------------- signature verification


def test_signed_delivery_accepted_and_logged(signed_client: TestClient, signed_app) -> None:
    payload = sample_text_payload()
    raw = json.dumps(payload).encode()
    resp = signed_client.post(
        "/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_payload(APP_SECRET, raw),
        },
    )
    assert resp.status_code == 200
    assert resp.json()["received"] == 1
    assert count_messages(db_path(signed_app)) == 1


def test_missing_signature_rejected_before_any_processing(
    signed_client: TestClient, signed_app
) -> None:
    resp = signed_client.post(
        "/webhook",
        content=json.dumps(sample_text_payload()).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 403
    # Rejected before parse/log/process: nothing hit the store.
    assert count_messages(db_path(signed_app)) == 0


def test_forged_signature_rejected_before_any_processing(
    signed_client: TestClient, signed_app
) -> None:
    payload = sample_text_payload()
    raw = json.dumps(payload).encode()
    forged = sign_payload("attacker-secret", raw)
    resp = signed_client.post(
        "/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": forged,
        },
    )
    assert resp.status_code == 403
    assert count_messages(db_path(signed_app)) == 0


def test_signature_bound_to_exact_body(signed_client: TestClient, signed_app) -> None:
    """A signature is only valid for the exact bytes it was computed over."""
    payload = sample_text_payload()
    raw = json.dumps(payload, separators=(",", ":")).encode()
    # Same payload, different serialization -> different bytes -> rejected.
    resp = signed_client.post(
        "/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sign_payload(APP_SECRET, json.dumps(payload).encode()),
        },
    )
    assert resp.status_code == 403
    assert count_messages(db_path(signed_app)) == 0


def test_verification_is_off_when_no_secret_configured(client: TestClient, app) -> None:
    """Dev default: no app secret -> deliveries accepted without a signature."""
    resp = client.post("/webhook", json=sample_text_payload())
    assert resp.status_code == 200
    assert count_messages(db_path(app)) == 1


def test_health_requires_auth(client: TestClient) -> None:
    """Health endpoint must reject unauthenticated requests."""
    resp = client.get("/health")
    assert resp.status_code == 401


def test_health(client: TestClient, app) -> None:
    headers = {"Authorization": f"Bearer {app.state.admin_token}"}
    resp = client.get("/health", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
