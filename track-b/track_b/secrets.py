"""Encrypted-at-rest credential storage + log scrubbing.

Application passwords are secrets: they must never live in a database,
config file, or log in plaintext. This module provides:

- `Vault` — Fernet (symmetric, AES-128-CBC + HMAC) encryption. The key
  comes from the `WPBOT_SECRETS_KEY` environment variable (a base64
  Fernet key, `python -c "from cryptography.fernet import Fernet;
  print(Fernet.generate_key().decode())"`). A fresh random key is
  generated in memory for local dev when the env var is missing — but
  that key is lost on restart, so production MUST set `WPBOT_SECRETS_KEY`
  or stored credentials become undecryptable.
- `CredentialStore` — SQLite table mapping site_url -> (username,
  encrypted application password). Only ciphertext touches disk.
- `redact()` — strips anything that looks like a password from a string
  before it goes into an error message or log line.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("track_b.secrets")

_PASSWORD_LIKE = re.compile(
    r"(?i)(password|passwd|pwd|app_pass|app-password|secret|token)\s*[:=]\s*\S+"
)
_CREDENTIALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_credentials (
    site_url          TEXT PRIMARY KEY,
    username          TEXT NOT NULL,
    encrypted_password TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
"""


def _default_key() -> bytes:
    """Dev-only key: random per process, so nothing persists. Production
    must set WPBOT_SECRETS_KEY explicitly (documented in the README)."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key()


class Vault:
    def __init__(self, key: bytes | None = None) -> None:
        from cryptography.fernet import Fernet

        key = key or os.environ.get("WPBOT_SECRETS_KEY") or _default_key()
        if isinstance(key, str):
            key = key.encode()
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()


def redact(text: str) -> str:
    """Mask credential-like values so they never reach logs/errors."""
    if not text:
        return text
    return _PASSWORD_LIKE.sub(lambda m: f"{m.group(1)}=***REDACTED***", text)


@dataclass(frozen=True)
class SiteCredentials:
    site_url: str
    username: str
    encrypted_password: str  # Fernet token — never plaintext


class CredentialStore:
    """SQLite-backed store of per-site WordPress credentials (ciphertext only)."""

    def __init__(self, db_path: Path, vault: Vault | None = None) -> None:
        self.db_path = db_path
        self.vault = vault or Vault()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_CREDENTIALS_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def set_credentials(self, site_url: str, username: str, app_password: str) -> None:
        """Encrypt and persist. Never stores plaintext."""
        token = self.vault.encrypt(app_password)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO site_credentials (site_url, username, encrypted_password, created_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(site_url) DO UPDATE SET
                    username = excluded.username,
                    encrypted_password = excluded.encrypted_password
                """,
                (site_url, username, token),
            )

    def get_credentials(self, site_url: str) -> tuple[str, str] | None:
        """Return (username, decrypted app password), or None if unknown."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username, encrypted_password FROM site_credentials "
                "WHERE site_url = ?",
                (site_url,),
            ).fetchone()
        if row is None:
            return None
        return row[0], self.vault.decrypt(row[1])

    def delete_credentials(self, site_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM site_credentials WHERE site_url = ?", (site_url,)
            )

    def list_sites(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT site_url FROM site_credentials").fetchall()
        return [r[0] for r in rows]
