"""Onboarding validation (PRD §12 step 3).

Valid credentials succeed and persist an onboarded site; invalid
credentials, unreachable sites, non-WordPress URLs, and users without
edit rights each fail with a specific reason — and a failed onboarding
persists nothing.
"""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from track_b.allowlist import PILOT_SITE_CONFIG
from track_b.main import create_app
from track_b.onboarding import (
    OnboardedSiteStore,
    onboard_site,
    validate_site_access,
)
from track_b.wordpress import WordPressClient
from wp_fake import SITE, FakeWordPress

OWNER = "15551234567"
USERNAME = "editor"
APP_PASSWORD = "SuperSecretAppPass123"


def run(coro):
    return asyncio.run(coro)


def make_client(fake: FakeWordPress, username=USERNAME, password=APP_PASSWORD):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return WordPressClient(SITE, username, password, client=http)


@pytest.fixture()
def store(tmp_path) -> OnboardedSiteStore:
    return OnboardedSiteStore(tmp_path / "onboard.db")


def test_valid_credentials_succeed_and_persist(store, tmp_path):
    fake = FakeWordPress(expected_auth=(USERNAME, APP_PASSWORD))
    result = run(onboard_site(SITE, USERNAME, APP_PASSWORD, OWNER, store=store, client=make_client(fake)))

    assert result.status == "success"
    assert result.reason == "ok"
    assert result.site_id and result.site_id.startswith("site-")

    site = store.get_site(result.site_id)
    assert site.owner_id == OWNER
    assert site.site_url == SITE
    assert site.status == "active"
    # Default B2 allowlist config was persisted.
    assert site.allowlist.site_url == PILOT_SITE_CONFIG.site_url
    assert site.allowlist.mappings["job"].enabled is True
    assert site.allowlist.mappings["image"].enabled is False  # v1.5 off by default
    # Application password never touches disk in plaintext.
    assert APP_PASSWORD.encode() not in (tmp_path / "onboard.db").read_bytes()


def test_invalid_credentials_fail_with_specific_reason(store):
    fake = FakeWordPress(expected_auth=(USERNAME, APP_PASSWORD))
    result = run(
        onboard_site(
            SITE, USERNAME, "WrongPassword123", OWNER,
            store=store, client=make_client(fake, password="WrongPassword123"),
        )
    )
    assert result.status == "failed"
    assert result.reason == "invalid_credentials"
    assert store.sites_for_owner(OWNER) == []  # nothing persisted


def test_unreachable_site_fails_with_specific_reason(store):
    fake = FakeWordPress()
    fake.connect_error = True
    result = run(onboard_site(SITE, USERNAME, APP_PASSWORD, OWNER, store=store, client=make_client(fake)))
    assert result.status == "failed"
    assert result.reason == "unreachable"


def test_not_wordpress_url_fails_with_specific_reason(store):
    fake = FakeWordPress()
    fake.inject_status = 404
    result = run(onboard_site(SITE, USERNAME, APP_PASSWORD, OWNER, store=store, client=make_client(fake)))
    assert result.status == "failed"
    assert result.reason == "not_wordpress"


def test_insufficient_permissions_fail_with_specific_reason(store):
    fake = FakeWordPress(
        expected_auth=(USERNAME, APP_PASSWORD), user_roles=("subscriber",)
    )
    result = run(onboard_site(SITE, USERNAME, APP_PASSWORD, OWNER, store=store, client=make_client(fake)))
    assert result.status == "failed"
    assert result.reason == "insufficient_permissions"
    assert "Editor" in result.message
    assert store.sites_for_owner(OWNER) == []


def test_invalid_url_fails_before_any_http(store):
    result = run(onboard_site("not a url at all", USERNAME, APP_PASSWORD, OWNER, store=store))
    assert result.status == "failed"
    assert result.reason == "invalid_url"
    assert store.sites_for_owner(OWNER) == []


def test_reboarding_same_url_refreshes_record(store):
    fake = FakeWordPress(expected_auth=(USERNAME, APP_PASSWORD))
    first = run(onboard_site(SITE, USERNAME, APP_PASSWORD, OWNER, store=store, client=make_client(fake)))
    # Re-onboard with a new password: the site now accepts it.
    fake2 = FakeWordPress(expected_auth=(USERNAME, "NewPassword456"))
    second = run(onboard_site(SITE, USERNAME, "NewPassword456", OWNER, store=store, client=make_client(fake2, password="NewPassword456")))
    assert second.status == "success"
    assert second.site_id == first.site_id  # same site, refreshed
    assert len(store.sites_for_owner(OWNER)) == 1


def test_validate_site_access_reports_roles_on_success(store):
    fake = FakeWordPress(expected_auth=(USERNAME, APP_PASSWORD))
    result = run(validate_site_access(SITE, USERNAME, APP_PASSWORD, client=make_client(fake)))
    assert result.ok is True
    assert result.roles == ("editor",)
    assert result.capabilities.get("edit_posts") is True


# ------------------------------------------------------- endpoint


def test_onboard_endpoint_success():
    from track_b.onboarding import OnboardResult

    async def runner(site_url, username, app_password, owner_id):
        return OnboardResult(status="success", reason="ok", site_id="site-abc", site_url=site_url)

    app = create_app(onboarding_runner=runner)
    client = TestClient(app)
    resp = client.post(
        "/sites/onboard",
        json={"site_url": SITE, "username": "editor", "app_password": "pw", "owner_id": OWNER},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["site_id"] == "site-abc"


def test_onboard_endpoint_failure_reports_reason():
    from track_b.onboarding import OnboardResult

    async def runner(site_url, username, app_password, owner_id):
        return OnboardResult(
            status="failed",
            reason="insufficient_permissions",
            message="the user lacks editing rights",
        )

    client = TestClient(create_app(onboarding_runner=runner))
    resp = client.post(
        "/sites/onboard",
        json={"site_url": SITE, "username": "subscriber", "app_password": "pw", "owner_id": OWNER},
    )
    assert resp.status_code == 422
    body = resp.json()["detail"]
    assert body["status"] == "failed"
    assert body["reason"] == "insufficient_permissions"
