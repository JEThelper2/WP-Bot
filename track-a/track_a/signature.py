"""Webhook signature verification (Meta `X-Hub-Signature-256`).

Meta signs every webhook delivery with the app secret:

    X-Hub-Signature-256: sha256=<hex HMAC-SHA256(app_secret, raw_body)>

The signature is over the **raw request body bytes** — verifying against
anything else (a re-serialized dict, the parsed JSON) silently fails
forever, so the caller must pass the exact body it received.

Verification is constant-time (`hmac.compare_digest`), so an attacker
cannot learn the digest byte-by-byte through timing. When
`WHATSAPP_APP_SECRET` is configured, the webhook rejects deliveries with
a missing or mismatched signature *before* the payload is parsed, logged,
or processed — a forged message never reaches the conversation.
"""

from __future__ import annotations

import hashlib
import hmac

_PREFIX = "sha256="


def verify_webhook_signature(
    app_secret: str,
    raw_body: bytes,
    signature_header: str | None,
) -> bool:
    """True only if `signature_header` is a valid HMAC-SHA256 of the body.

    Returns False for a missing/malformed header, an empty secret, or any
    mismatch — never raises.
    """
    if not app_secret or not raw_body or not signature_header:
        return False
    if not signature_header.startswith(_PREFIX):
        return False
    expected = signature_header[len(_PREFIX) :].strip()
    if not expected:
        return False

    computed = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, expected)
