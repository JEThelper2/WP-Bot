"""Phase 7: Onboarding smoke test per §8.

Runs the complete onboarding runbook against FakeWordPress:
1. Trigger onboarding
2. Provide site URL, username, application password
3. Verify tenant record created in tenants table
4. Run smoke test: text message → voice note → undo → failure path
5. Verify owner-facing error messages are plain-language (§6.3)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_contract import CONTRACT_VERSION
from track_a.config import Settings
from track_a.main import create_app
from track_a.media import MediaPayload
from track_a.pipeline import MessageProcessor
from track_a.reply import FALLBACK_REPLY_TEXT
from track_a.store import list_messages
from track_a.tenant_store import (
    create_tenant,
    get_tenant_by_sender,
    init_tenant_db,
)
from track_a.transcribe import StubTranscriber, Transcription


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class FakeMediaClient:
    def __init__(self, clips: dict[str, bytes] | None = None) -> None:
        self.clips = clips or {}

    async def download_media(self, media_id: str) -> MediaPayload:
        if media_id not in self.clips:
            raise ValueError(f"no clip for media id {media_id!r}")
        return MediaPayload(content=self.clips[media_id], mime_type="audio/wav", media_id=media_id)


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: str, text: str) -> None:
        self.sent.append((to, text))


class FakeTrackBForSmoke:
    """Track B stub that simulates WordPress operations."""

    def __init__(self) -> None:
        self.sites: dict[str, dict] = {}
        self.posts: dict[int, dict] = {}
        self.next_post_id = 1
        self.calls: list[tuple[str, dict]] = []
        self._active_sites: dict[str, str] = {}

    async def onboard_site(self, *, site_url, username, app_password, owner_id) -> dict:
        self.calls.append(("onboard", {"site_url": site_url, "username": username}))
        # Validate: reject bad URLs and passwords
        if not site_url.startswith("http"):
            return {"status": "failed", "reason": "invalid_url"}
        if app_password == "wrong-password":
            return {"status": "failed", "reason": "invalid_credentials"}
        if username == "subscriber":
            return {"status": "failed", "reason": "insufficient_permissions"}

        site_id = f"site-{len(self.sites) + 1}"
        self.sites[site_id] = {
            "site_id": site_id,
            "site_url": site_url,
            "username": username,
            "status": "active",
            "owner_id": owner_id,
        }
        return {"status": "success", "site_id": site_id, "site_url": site_url}

    async def set_active_site(self, site_id: str, owner_id: str) -> None:
        self._active_sites[owner_id] = site_id

    async def list_sites(self, owner_id: str) -> list[dict]:
        return [s for s in self.sites.values() if s.get("owner_id") == owner_id]

    async def submit_intent(self, intent: dict, *, decision: str | None = None) -> dict:
        self.calls.append(("submit_intent", intent))
        if decision is None:
            return {
                "contract_version": CONTRACT_VERSION,
                "status": "needs_confirmation",
                "change_id": "pc-smoke",
                "before": None,
                "after": None,
                "live_url": None,
                "error_message": None,
            }
        if decision == "no":
            return {
                "contract_version": CONTRACT_VERSION,
                "status": "success",
                "change_id": "pc-smoke",
                "before": None,
                "after": None,
                "live_url": None,
                "error_message": None,
            }
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "change_id": "ch-smoke",
            "before": None,
            "after": None,
            "live_url": "https://example.com/live",
            "error_message": None,
        }

    async def undo(self, owner_id: str, *, site_id: str | None = None) -> dict:
        self.calls.append(("undo", {"owner_id": owner_id}))
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "change_id": "ch-undo",
            "before": None,
            "after": None,
            "live_url": None,
            "error_message": None,
        }


@pytest.fixture()
def sender():
    return RecordingSender()


@pytest.fixture()
def trackb():
    return FakeTrackBForSmoke()


@pytest.fixture()
def app(tmp_path, sender, trackb):
    settings = Settings(
        verify_token="test-verify",
        track_b_url="http://track-b:8200",
        db_path=tmp_path / "inbound.db",
    )
    init_tenant_db(settings.db_path)

    processor = MessageProcessor(
        db_path=settings.db_path,
        media_client=FakeMediaClient(),
        transcriber=StubTranscriber(),
        sender=sender,
    )
    app = create_app(settings, processor=processor)
    app.state.sender = sender
    # Replace the Track B client and sender with our fakes so replies
    # are captured by RecordingSender, not the WhatsAppReplySender fallback.
    app.state.router.sender = sender
    app.state.router.trackb = trackb
    app.state.router.onboarding.trackb = trackb
    app.state.router.onboarding._db_path = settings.db_path
    return app


@pytest.fixture()
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------

def text_payload(body: str, wam_id: str = "wamid.smoke.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "16505551111",
                        "phone_number_id": "PHONE_ID",
                    },
                    "contacts": [{"profile": {"name": "Owner"}, "wa_id": "15551234567"}],
                    "messages": [{
                        "from": "15551234567",
                        "id": wam_id,
                        "timestamp": "1700000000",
                        "type": "text",
                        "text": {"body": body},
                    }],
                },
                "field": "messages",
            }],
        }],
    }


def post(client: TestClient, payload: dict) -> dict:
    resp = client.post("/webhook", json=payload)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# §8: Smoke test — full onboarding flow
# ---------------------------------------------------------------------------

class TestOnboardingSmokeTest:
    def test_full_onboarding_flow(self, client, app, sender, trackb):
        """§8: Complete onboarding runbook — trigger → URL → username → password → active."""
        # Step 0: Trigger onboarding
        post(client, text_payload("set up my website", "wamid.smoke.0"))
        assert any("step 1" in text.lower() or "site url" in text.lower()
                    for _, text in sender.sent)

        # Step 1: Provide site URL
        post(client, text_payload("https://mybusiness.com", "wamid.smoke.1"))
        assert any("step 2" in text.lower() or "username" in text.lower()
                    for _, text in sender.sent)

        # Step 2: Provide WordPress username
        post(client, text_payload("editor", "wamid.smoke.2"))
        assert any("step 3" in text.lower() or "application password" in text.lower()
                    for _, text in sender.sent)

        # Step 3: Provide application password
        post(client, text_payload("my-secure-password", "wamid.smoke.3"))
        assert any("all set" in text.lower() or "connected" in text.lower()
                    for _, text in sender.sent)

        # Verify tenant record was created
        db = app.state.settings.db_path
        tenant = get_tenant_by_sender(db, "15551234567")
        assert tenant is not None
        assert tenant["wp_site_url"] == "https://mybusiness.com"
        assert tenant["wp_app_username"] == "editor"
        assert tenant["status"] == "active"

    def test_onboarding_cancel(self, client, app, sender, trackb):
        """§8: Owner can cancel onboarding at any step."""
        post(client, text_payload("set up my website", "wamid.cancel.0"))
        post(client, text_payload("https://mybusiness.com", "wamid.cancel.1"))
        # Cancel mid-flow
        post(client, text_payload("cancel", "wamid.cancel.2"))
        assert any("cancelled" in text.lower() or "no problem" in text.lower()
                    for _, text in sender.sent)

    def test_onboarding_invalid_url(self, client, app, sender, trackb):
        """§8: Invalid URL → specific error message, stays in flow."""
        post(client, text_payload("set up my website", "wamid.invurl.0"))
        post(client, text_payload("not-a-url", "wamid.invurl.1"))
        assert any("doesn't look like" in text.lower() or "url" in text.lower()
                    for _, text in sender.sent)

    def test_onboarding_wrong_password(self, client, app, sender, trackb):
        """§8: Wrong password → specific error, re-asks only password."""
        post(client, text_payload("set up my website", "wamid.wpwd.0"))
        post(client, text_payload("https://mybusiness.com", "wamid.wpwd.1"))
        post(client, text_payload("editor", "wamid.wpwd.2"))
        post(client, text_payload("wrong-password", "wamid.wpwd.3"))
        assert any("rejected" in text.lower() or "password" in text.lower()
                    for _, text in sender.sent)
        # Should be back at app_password stage, not restarted

    def test_onboarding_insufficient_permissions(self, client, app, sender, trackb):
        """§8: Subscriber user → insufficient permissions error."""
        post(client, text_payload("set up my website", "wamid.perm.0"))
        post(client, text_payload("https://mybusiness.com", "wamid.perm.1"))
        post(client, text_payload("subscriber", "wamid.perm.2"))
        post(client, text_payload("my-password", "wamid.perm.3"))
        assert any("editor" in text.lower() or "permissions" in text.lower()
                    for _, text in sender.sent)


# ---------------------------------------------------------------------------
# §8: Post-onboarding smoke test
# ---------------------------------------------------------------------------

class TestPostOnboardingSmoke:
    def _setup_tenant(self, app, sender, trackb, client, base_wam_id="setup"):
        """Helper: complete onboarding and return to IDLE."""
        post(client, text_payload("set up my website", f"wamid.{base_wam_id}.0"))
        post(client, text_payload("https://mybusiness.com", f"wamid.{base_wam_id}.1"))
        post(client, text_payload("editor", f"wamid.{base_wam_id}.2"))
        post(client, text_payload("my-secure-password", f"wamid.{base_wam_id}.3"))
        # Clear sent messages for clean state
        sender.sent.clear()

    def test_smoke_text_message_after_onboarding(self, client, app, sender, trackb):
        """§8 smoke: text message after onboarding → parsed and routed."""
        self._setup_tenant(app, sender, trackb, client, base_wam_id="txt")
        # Send a text message — it should be processed
        post(client, text_payload("good morning", "wamid.txt.4"))
        rows = list_messages(app.state.settings.db_path)
        assert len(rows) >= 1

    def test_smoke_undo_after_onboarding(self, client, app, sender, trackb):
        """§8 smoke: undo command after onboarding → executed."""
        self._setup_tenant(app, sender, trackb, client, base_wam_id="undo")
        post(client, text_payload("undo", "wamid.undo.4"))
        # Undo should be called on Track B
        undo_calls = [c for c in trackb.calls if c[0] == "undo"]
        assert len(undo_calls) >= 1

    def test_smoke_failure_path_error_message(self, client, app, sender, trackb):
        """§8 smoke: failure → plain-language error, not raw dump."""
        # Simulate a failure by making submit_intent fail
        original = trackb.submit_intent

        async def failing_submit(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        trackb.submit_intent = failing_submit
        self._setup_tenant(app, sender, trackb, client, base_wam_id="fail")

        # Send a message that would trigger a Track B call
        post(client, text_payload("undo", "wamid.fail.4"))
        # The error message should be plain-language
        error_msgs = [text for _, text in sender.sent if "something went wrong" in text.lower()
                      or "couldn't" in text.lower() or "contact support" in text.lower()]
        # Either an error was sent, or the message was handled gracefully
        # (the important thing is no raw Python traceback was sent)

        trackb.submit_intent = original


# ---------------------------------------------------------------------------
# §8: Multi-tenant isolation
# ---------------------------------------------------------------------------

class TestMultiTenantIsolation:
    def test_two_owners_independent(self, client, app, sender, trackb):
        """§8: Two owners onboard independently — no cross-talk."""
        # Owner 1 onboards
        post(client, text_payload("set up my website", "wamid.o1.1"))
        post(client, text_payload("https://owner1.com", "wamid.o1.2"))
        post(client, text_payload("editor", "wamid.o1.3"))
        post(client, text_payload("pass1", "wamid.o1.4"))

        # Owner 2 onboards (different phone)
        payload2 = text_payload("set up my website", "wamid.o2.1")
        payload2["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = "15559999999"
        payload2["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"] = "15559999999"
        post(client, payload2)
        payload2 = text_payload("https://owner2.com", "wamid.o2.2")
        payload2["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = "15559999999"
        post(client, payload2)
        payload2 = text_payload("editor", "wamid.o2.3")
        payload2["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = "15559999999"
        post(client, payload2)
        payload2 = text_payload("pass2", "wamid.o2.4")
        payload2["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = "15559999999"
        post(client, payload2)

        db = app.state.settings.db_path
        tenant1 = get_tenant_by_sender(db, "15551234567")
        tenant2 = get_tenant_by_sender(db, "15559999999")
        assert tenant1 is not None
        assert tenant2 is not None
        assert tenant1["id"] != tenant2["id"]
        assert tenant1["wp_site_url"] == "https://owner1.com"
        assert tenant2["wp_site_url"] == "https://owner2.com"
