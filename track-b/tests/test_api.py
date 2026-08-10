"""End-to-end API tests (Track B only, no WhatsApp).

The full lifecycle through the REAL endpoints:
submit intent -> needs_confirmation (staged) -> resolve yes -> success
result with before/after/live_url -> undo -> success result reversing the
change. Every response body is validated against result.schema.json.
"""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from shared_contract import CONTRACT_VERSION, validate_result

from track_b.allowlist import PILOT_SITE_CONFIG
from track_b.changelog import InMemoryChangeLog
from track_b.main import TrackBServices, create_app
from track_b.onboarding import OnboardedSiteStore
from track_b.pending import InMemoryPendingStore
from track_b.wordpress import WordPressClient
from wp_fake import SITE, FakeWordPress

OWNER = "15551234567"
USER = "editor"
APP_PASSWORD = "SuperSecretAppPass123"


def make_intent(action="create", content_type="job", fields=None, **extra):
    intent = {
        "contract_version": CONTRACT_VERSION,
        "owner_id": OWNER,
        "action": action,
        "content_type": content_type,
        "fields": fields or {"title": "Barista", "description": "$18/hr"},
        "confidence": 0.95,
    }
    intent.update(extra)
    return intent


def assert_valid_result(body: dict) -> None:
    validate_result(body)  # the boundary discipline: always contract-valid


@pytest.fixture()
def client_with(tmp_path):
    """Build an app wired to a fake WordPress + injected services."""

    def build(fake: FakeWordPress) -> TestClient:
        store = OnboardedSiteStore(tmp_path / "api.db")
        store.add_site(
            owner_id=OWNER,
            site_url=SITE,
            username=USER,
            app_password=APP_PASSWORD,
            allowlist=PILOT_SITE_CONFIG,
        )
        pending = InMemoryPendingStore()
        changelog = InMemoryChangeLog()

        def make_client(site):
            creds = store.credentials_for(site.site_id)
            http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
            return WordPressClient(site.site_url, creds[0], creds[1], client=http)

        services = TrackBServices(
            sites=store, pending=pending, changelog=changelog, make_client=make_client
        )
        return TestClient(create_app(services=services))

    return build


# ------------------------------------------------------- the full lifecycle


def test_full_lifecycle_stage_resolve_write_undo(client_with):
    fake = FakeWordPress(expected_auth=(USER, APP_PASSWORD))
    client = client_with(fake)

    # 1. submit intent -> staged, needs_confirmation
    staged = client.post("/intent", json=make_intent())
    assert staged.status_code == 200
    body = staged.json()
    assert_valid_result(body)
    assert body["status"] == "needs_confirmation"
    pending_change_id = body["change_id"]
    assert pending_change_id.startswith("pc-")

    # 2. resolve YES -> write through B2/B1, logged by B4
    resolved = client.post(f"/intent?decision=yes", json=make_intent())
    assert resolved.status_code == 200
    body = resolved.json()
    assert_valid_result(body)
    assert body["status"] == "success"
    assert body["change_id"] == pending_change_id  # trail links stage -> write
    assert body["before"] is None
    assert body["after"]["title"] == "Barista"
    assert body["after"]["status"] == "publish"
    assert body["live_url"] == f"{SITE}/?p=1"

    # 3. undo -> reverses the create (deletes the post), logged
    undone = client.post("/undo", json={"owner_id": OWNER})
    assert undone.status_code == 200
    body = undone.json()
    assert_valid_result(body)
    assert body["status"] == "success"
    assert body["change_id"].startswith("ch-")
    assert body["after"]["deleted"] is True
    assert fake.posts[1]["status"] == "trash"

    # audit trail: create row + undo row, linked
    services: TrackBServices = client.app.state.services
    recent = asyncio.run(services.changelog.most_recent(OWNER))
    assert recent.action == "undo"
    assert recent.undo_of == pending_change_id


# ------------------------------------------------------- resolution paths


def test_resolve_no_discards_without_writing(client_with):
    fake = FakeWordPress(expected_auth=(USER, APP_PASSWORD))
    client = client_with(fake)

    client.post("/intent", json=make_intent())
    declined = client.post("/intent?decision=no", json=make_intent())
    assert declined.status_code == 200
    body = declined.json()
    assert_valid_result(body)
    assert body["status"] == "success"
    assert body["after"] is None  # nothing was written

    assert len(fake.posts) == 0  # no WordPress write ever happened


def test_resolve_with_nothing_pending_is_clear(client_with):
    client = client_with(FakeWordPress())
    resp = client.post("/intent?decision=yes", json=make_intent())
    assert resp.status_code == 422
    body = resp.json()
    assert_valid_result(body)
    assert body["status"] == "failed"
    assert "nothing is pending" in body["error_message"]


def test_invalid_decision_value_rejected(client_with):
    client = client_with(FakeWordPress())
    resp = client.post("/intent?decision=maybe", json=make_intent())
    assert resp.status_code == 422
    body = resp.json()
    assert_valid_result(body)
    assert "decision" in body["error_message"]


def test_resolve_yes_without_onboarded_site_fails(client_with, tmp_path):
    """A different owner staged an intent but has no onboarded site."""
    client = client_with(FakeWordPress())  # OWNER is onboarded; use another
    other = "15559999999"
    intent = make_intent()
    intent["owner_id"] = other
    client.post("/intent", json=intent)

    resp = client.post("/intent?decision=yes", json=intent)
    assert resp.status_code == 422
    body = resp.json()
    assert_valid_result(body)
    assert "no onboarded site" in body["error_message"]


# ------------------------------------------------------- gate at write time


def test_disabled_content_type_rejected_at_write(client_with):
    fake = FakeWordPress(expected_auth=(USER, APP_PASSWORD))
    client = client_with(fake)

    image_intent = make_intent(
        "update", "image", {"slot": "logo", "media_base64": "AA=="}
    )
    staged = client.post("/intent", json=image_intent)
    assert staged.json()["status"] == "needs_confirmation"

    resp = client.post("/intent?decision=yes", json=image_intent)
    assert resp.status_code == 422
    body = resp.json()
    assert_valid_result(body)
    assert body["status"] == "failed"
    assert "not enabled" in body["error_message"]
    assert len(fake.posts) == 0 and len(fake.media) == 0  # nothing written


# ------------------------------------------------------- schema validation


def test_malformed_intent_rejected_before_anything(client_with):
    client = client_with(FakeWordPress())
    bad = make_intent()
    del bad["confidence"]

    resp = client.post("/intent", json=bad)
    assert resp.status_code == 422
    body = resp.json()
    assert_valid_result(body)
    assert body["status"] == "failed"
    assert "confidence" in body["error_message"]


def test_health(client_with):
    client = client_with(FakeWordPress())
    assert client.get("/health").json()["status"] == "ok"
