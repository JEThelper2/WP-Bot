"""Unit tests for X-Hub-Signature-256 verification (HMAC-SHA256)."""

import hashlib
import hmac

from track_a.signature import verify_webhook_signature

SECRET = "my-app-secret"
BODY = b'{"object": "whatsapp_business_account", "entry": []}'


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_passes() -> None:
    assert verify_webhook_signature(SECRET, BODY, sign(SECRET, BODY)) is True


def test_wrong_secret_fails() -> None:
    assert verify_webhook_signature(SECRET, BODY, sign("wrong-secret", BODY)) is False


def test_tampered_body_fails() -> None:
    tampered = BODY + b" "
    assert verify_webhook_signature(SECRET, tampered, sign(SECRET, BODY)) is False


def test_missing_header_fails() -> None:
    assert verify_webhook_signature(SECRET, BODY, None) is False


def test_malformed_header_fails() -> None:
    assert verify_webhook_signature(SECRET, BODY, "sha256=") is False
    assert verify_webhook_signature(SECRET, BODY, "md5=abc123") is False
    assert verify_webhook_signature(SECRET, BODY, "garbage") is False


def test_empty_secret_fails() -> None:
    # Defensive: verification with no secret never passes, so a miswired
    # caller can't accidentally accept everything.
    assert verify_webhook_signature("", BODY, sign(SECRET, BODY)) is False


def test_empty_body_fails() -> None:
    assert verify_webhook_signature(SECRET, b"", sign(SECRET, BODY)) is False
