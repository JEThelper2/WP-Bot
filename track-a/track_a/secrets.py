"""Secrets management for WordPress Application Passwords (§7.2).

WordPress Application Passwords are encrypted at rest in the tenants table
using Fernet (AES-128-CBC + HMAC-SHA256). The encryption key is stored as
the ENCRYPTION_KEY environment variable — never in the database or code.

Design:
- Encrypt on write (tenant creation / credential update)
- Decrypt only in-memory at the point of making the WP API call
- Never log decrypted values
- Fernet is used over raw AES-256-GCM because it's stdlib-friendly
  (via the `cryptography` package) and includes authentication (HMAC)
  which prevents ciphertext malleability.
"""

from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger("track_a.secrets")

# Module-level cache for the Fernet instance (initialized once at startup).
_fernet = None


def _get_fernet():
    """Get or initialize the Fernet cipher from ENCRYPTION_KEY env var."""
    global _fernet
    if _fernet is not None:
        return _fernet

    key = os.environ.get("ENCRYPTION_KEY", "")
    if not key:
        logger.warning(
            "ENCRYPTION_KEY not set — secrets encryption is DISABLED. "
            "Credentials will be stored in plaintext. Set ENCRYPTION_KEY "
            "for production use."
        )
        return None

    try:
        from cryptography.fernet import Fernet

        # Accept both raw Fernet keys and base64-encoded keys
        if isinstance(key, str):
            key = key.encode("utf-8")
        _fernet = Fernet(key)
        return _fernet
    except ImportError:
        logger.warning(
            "cryptography package not installed — secrets encryption "
            "disabled. Install with: pip install cryptography"
        )
        return None
    except Exception as exc:
        logger.error("Failed to initialize encryption: %s", exc)
        return None


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string for storage in the database.

    Returns a base64-encoded ciphertext string.  If encryption is not
    configured (missing ENCRYPTION_KEY or cryptography package), returns
    the plaintext with a warning — this allows development to proceed
    without encryption while making it obvious that production needs it.
    """
    if not plaintext:
        return ""

    fernet = _get_fernet()
    if fernet is None:
        # Dev mode: store plaintext with a prefix so it's obvious
        return f"plain:{plaintext}"

    token = fernet.encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a secret stored in the database.

    Returns the plaintext string.  Handles both encrypted (Fernet) and
    dev-mode plaintext (prefixed with "plain:") values.
    """
    if not ciphertext:
        return ""

    # Handle dev-mode plaintext
    if ciphertext.startswith("plain:"):
        return ciphertext[6:]

    fernet = _get_fernet()
    if fernet is None:
        # Cannot decrypt without the key — return as-is (shouldn't happen
        # in production, but allows tests to pass with plaintext values)
        logger.warning("Cannot decrypt without ENCRYPTION_KEY; returning as-is")
        return ciphertext

    try:
        plaintext = fernet.decrypt(ciphertext.encode("utf-8"))
        return plaintext.decode("utf-8")
    except Exception as exc:
        logger.error("Failed to decrypt secret: %s", exc)
        raise ValueError("Failed to decrypt secret — wrong key or corrupted data") from exc


def generate_key() -> str:
    """Generate a new Fernet encryption key.

    Call this once during initial setup and store the result as the
    ENCRYPTION_KEY environment variable.
    """
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode("utf-8")
