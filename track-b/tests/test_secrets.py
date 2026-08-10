"""Encrypted credential storage: roundtrip, wrong-key failure, ciphertext
at rest, and redaction of credential-like values from error text."""

import pytest
from cryptography.fernet import Fernet, InvalidToken

from track_b.secrets import CredentialStore, Vault, redact

PASSWORD = "SuperSecretAppPass123"


def test_vault_roundtrip():
    vault = Vault(Fernet.generate_key())
    token = vault.encrypt(PASSWORD)
    assert token != PASSWORD
    assert vault.decrypt(token) == PASSWORD


def test_vault_wrong_key_cannot_decrypt():
    vault = Vault(Fernet.generate_key())
    token = vault.encrypt(PASSWORD)
    other = Vault(Fernet.generate_key())
    with pytest.raises(InvalidToken):
        other.decrypt(token)


def test_credential_store_roundtrip(tmp_path):
    store = CredentialStore(tmp_path / "creds.db", Vault(Fernet.generate_key()))
    store.set_credentials("https://wp.example.com", "editor", PASSWORD)
    assert store.get_credentials("https://wp.example.com") == ("editor", PASSWORD)
    store.delete_credentials("https://wp.example.com")
    assert store.get_credentials("https://wp.example.com") is None


def test_plaintext_never_reaches_disk(tmp_path):
    db = tmp_path / "creds.db"
    store = CredentialStore(db, Vault(Fernet.generate_key()))
    store.set_credentials("https://wp.example.com", "editor", PASSWORD)
    raw = db.read_bytes()
    assert PASSWORD.encode() not in raw
    assert b"editor" in raw  # username may be plaintext; password must not


def test_list_sites(tmp_path):
    store = CredentialStore(tmp_path / "creds.db", Vault(Fernet.generate_key()))
    store.set_credentials("https://a.example.com", "u1", "p1")
    store.set_credentials("https://b.example.com", "u2", "p2")
    assert set(store.list_sites()) == {"https://a.example.com", "https://b.example.com"}


def test_redact_masks_credential_like_values():
    assert "***REDACTED***" in redact("app_password=SuperSecret")
    assert "SuperSecret" not in redact("password: SuperSecret")
    assert redact("plain message without secrets") == "plain message without secrets"
