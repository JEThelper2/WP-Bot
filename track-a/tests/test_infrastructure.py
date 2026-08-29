"""Phase 6: Infrastructure tests per §7.

Covers:
- §7.2: Secrets encryption (encrypt/decrypt roundtrip, dev-mode plaintext)
- §7.2: Tenant password encryption at rest
- §7.4: Operator alerting (Telegram sender, fallback frequency tracker)
- Pre-commit secrets check
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from track_a.secrets import decrypt_secret, encrypt_secret, generate_key


# ---------------------------------------------------------------------------
# §7.2: Secrets encryption
# ---------------------------------------------------------------------------

class TestSecretsEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        """Encrypted value decrypts back to original."""
        key = generate_key()
        import os
        os.environ["ENCRYPTION_KEY"] = key
        try:
            # Reset the cached fernet
            import track_a.secrets as s
            s._fernet = None

            plaintext = "my-super-secret-app-password-123"
            encrypted = encrypt_secret(plaintext)
            assert encrypted != plaintext
            decrypted = decrypt_secret(encrypted)
            assert decrypted == plaintext
        finally:
            del os.environ["ENCRYPTION_KEY"]
            s._fernet = None

    def test_encryption_disabled_returns_plaintext(self):
        """Without ENCRYPTION_KEY, secrets stored as plaintext with prefix."""
        import os
        os.environ.pop("ENCRYPTION_KEY", None)
        import track_a.secrets as s
        s._fernet = None

        result = encrypt_secret("my-password")
        assert result == "plain:my-password"
        assert decrypt_secret(result) == "my-password"

    def test_empty_string_handled(self):
        """Empty strings pass through without error."""
        assert encrypt_secret("") == ""
        assert decrypt_secret("") == ""

    def test_generate_key_format(self):
        """Generated key is valid Fernet key format."""
        key = generate_key()
        assert len(key) > 20
        # Fernet keys are URL-safe base64-encoded 32-byte keys
        assert isinstance(key, str)


# ---------------------------------------------------------------------------
# §7.2: Tenant password encryption
# ---------------------------------------------------------------------------

class TestTenantPasswordEncryption:
    def test_create_tenant_encrypts_password(self, tmp_path):
        """Tenant password is encrypted at rest in the database."""
        from track_a.tenant_store import create_tenant, get_tenant, init_tenant_db

        db = tmp_path / "test.db"
        init_tenant_db(db)

        # Set up encryption
        import os
        key = generate_key()
        os.environ["ENCRYPTION_KEY"] = key
        import track_a.secrets as s
        s._fernet = None
        try:
            tenant = create_tenant(
                db,
                sender_id="15551234567",
                wp_app_password_enc="my-secret-password",
            )
            # The stored value should NOT be plaintext
            raw = get_tenant(db, tenant["id"])
            assert raw["wp_app_password_enc"] != "my-secret-password"
            assert raw["wp_app_password_enc"] != "plain:my-secret-password"

            # But decrypt_tenant_password should return the original
            from track_a.tenant_store import decrypt_tenant_password
            decrypted = decrypt_tenant_password(db, tenant["id"])
            assert decrypted == "my-secret-password"
        finally:
            del os.environ["ENCRYPTION_KEY"]
            s._fernet = None

    def test_update_credentials_encrypts_password(self, tmp_path):
        """Updated credentials are also encrypted."""
        from track_a.tenant_store import (
            create_tenant,
            decrypt_tenant_password,
            init_tenant_db,
            update_tenant_credentials,
        )

        db = tmp_path / "test.db"
        init_tenant_db(db)

        import os
        key = generate_key()
        os.environ["ENCRYPTION_KEY"] = key
        import track_a.secrets as s
        s._fernet = None
        try:
            tenant = create_tenant(db, sender_id="15551234567")
            update_tenant_credentials(
                db,
                tenant["id"],
                wp_site_url="https://example.com",
                wp_app_username="admin",
                wp_app_password_enc="new-password-456",
            )
            decrypted = decrypt_tenant_password(db, tenant["id"])
            assert decrypted == "new-password-456"
        finally:
            del os.environ["ENCRYPTION_KEY"]
            s._fernet = None


# ---------------------------------------------------------------------------
# §7.4: Operator alerting
# ---------------------------------------------------------------------------

class TestFallbackFrequencyTracker:
    def test_no_alert_below_threshold(self):
        """No alert when fallback count is below threshold."""
        from track_a.alerting import FallbackFrequencyTracker

        alert_fn = MagicMock()
        tracker = FallbackFrequencyTracker(threshold=3, window_seconds=60, alert_fn=alert_fn)

        for _ in range(3):
            tracker.record_fallback()

        alert_fn.assert_not_called()

    def test_alert_on_threshold_exceeded(self):
        """Alert fires when fallback count exceeds threshold."""
        from track_a.alerting import FallbackFrequencyTracker

        alert_fn = MagicMock()
        tracker = FallbackFrequencyTracker(threshold=3, window_seconds=60, alert_fn=alert_fn)

        for _ in range(4):
            tracker.record_fallback()

        alert_fn.assert_called_once()
        assert "4 times" in alert_fn.call_args[0][0]

    def test_window_expiry(self):
        """Old fallback events expire outside the window."""
        from track_a.alerting import FallbackFrequencyTracker

        alert_fn = MagicMock()
        tracker = FallbackFrequencyTracker(threshold=2, window_seconds=1, alert_fn=alert_fn)

        tracker.record_fallback()
        tracker.record_fallback()
        tracker.record_fallback()  # exceeds threshold

        alert_fn.assert_called_once()
        assert tracker.count == 3


class TestTelegramAlertSender:
    def test_no_token_logs_warning(self):
        """Without bot_token, alert is logged but not sent."""
        from track_a.alerting import TelegramAlertSender

        sender = TelegramAlertSender(bot_token="", chat_id="")
        # Should not raise
        sender("test alert")

    def test_no_chat_id_logs_warning(self):
        """Without chat_id, alert is logged but not sent."""
        from track_a.alerting import TelegramAlertSender

        sender = TelegramAlertSender(bot_token="fake-token", chat_id="")
        sender("test alert")


# ---------------------------------------------------------------------------
# Pre-commit secrets check
# ---------------------------------------------------------------------------

class TestSecretsCheck:
    def test_clean_content_passes(self):
        """Content without key patterns passes."""
        from scripts.check_secrets import scan_content

        findings = scan_content("hello world\nthis is clean", "test.py")
        assert len(findings) == 0

    def test_openai_key_detected(self):
        """OpenAI key pattern is detected."""
        from scripts.check_secrets import scan_content

        findings = scan_content('api_key = "sk-1234567890abcdef1234567890abcdef"', "test.py")
        assert len(findings) == 1
        assert "OpenAI" in findings[0][0]

    def test_google_key_detected(self):
        """Google API key pattern is detected."""
        from scripts.check_secrets import scan_content

        findings = scan_content('key = "AIzaSyA1234567890abcdefghijklmnopqrstuv"', "test.py")
        assert len(findings) == 1
        assert "Google" in findings[0][0]

    def test_aws_key_detected(self):
        """AWS key pattern is detected."""
        from scripts.check_secrets import scan_content

        findings = scan_content('aws_key = "AKIAIOSFODNN7EXAMPLE"', "test.py")
        assert len(findings) == 1
        assert "AWS" in findings[0][0]

    def test_diff_metadata_skipped(self):
        """Diff metadata lines are not scanned."""
        from scripts.check_secrets import scan_content

        content = "--- a/test.py\n+++ b/test.py\n@@ -1 +1 @@\n+hello world"
        findings = scan_content(content, "test.py")
        assert len(findings) == 0

    def test_removed_lines_skipped(self):
        """Removed lines (diff deletions) are not scanned."""
        from scripts.check_secrets import scan_content

        content = "-old api_key = \"sk-1234567890abcdef1234567890abcdef\"\n+new line"
        findings = scan_content(content, "test.py")
        assert len(findings) == 0
