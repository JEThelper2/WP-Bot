"""The PRD §12 onboarding conversation against the REAL WordPress sandbox.

Skipped unless a sandbox is running — same gating as
test_integration_wp.py:

    docker compose -f track-b/wp-sandbox/docker-compose.yml up -d
    docker compose -f track-b/wp-sandbox/docker-compose.yml logs -f setup

then:

    WPBOT_WP_TEST_URL=http://localhost:8090 \
    WPBOT_WP_TEST_USERNAME=editor \
    WPBOT_WP_TEST_APP_PASSWORD=$(cat track-b/wp-sandbox/_output/app-password.txt) \
    pytest track-b/tests/test_onboarding_sandbox.py -v

The sandbox user is EDITOR with an application password — never admin.
The B5 probe (`/sites/onboard`) hits the real WordPress REST API over
HTTP, so a passing run is a genuine "owner onboards a real site" check.
"""

import os
from pathlib import Path

import httpx
import pytest

from track_a.onboarding import (
    ONBOARD_INVALID_CREDENTIALS,
    ONBOARD_STEP_URL,
    ONBOARD_SUCCESS,
    ONBOARD_UNREACHABLE,
)

from integration_harness import OWNER, build_world, send

_SANDBOX_PASSWORD_FILE = (
    Path(__file__).resolve().parent.parent / "wp-sandbox" / "_output" / "app-password.txt"
)

WP_URL = os.environ.get("WPBOT_WP_TEST_URL", "")
WP_USERNAME = os.environ.get("WPBOT_WP_TEST_USERNAME", "editor")
WP_APP_PASSWORD = os.environ.get("WPBOT_WP_TEST_APP_PASSWORD", "")
if not WP_APP_PASSWORD and _SANDBOX_PASSWORD_FILE.exists():
    WP_APP_PASSWORD = _SANDBOX_PASSWORD_FILE.read_text().strip()


def _sandbox_available() -> bool:
    if not WP_URL:
        return False
    try:
        resp = httpx.get(f"{WP_URL}/wp-json/", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _sandbox_available(),
    reason="no WordPress sandbox running — see track-b/wp-sandbox/docker-compose.yml",
)


def test_onboarding_success_against_real_sandbox(tmp_path):
    world = build_world(tmp_path, seed_site=False, probe_through_fake=False)

    send(world.client, "set up my website", "wamid.sb.1")
    assert world.sender.sent[-1][1] == ONBOARD_STEP_URL

    send(world.client, WP_URL, "wamid.sb.2")
    send(world.client, WP_USERNAME, "wamid.sb.3")
    send(world.client, WP_APP_PASSWORD, "wamid.sb.4")

    # Real B5 probe succeeded against the sandbox.
    assert world.sender.sent[-1][1] == ONBOARD_SUCCESS.format(url=WP_URL)
    sites = world.services.sites.sites_for_owner(OWNER)
    assert len(sites) == 1
    assert sites[0].site_url == WP_URL
    assert sites[0].status == "active"


def test_onboarding_invalid_credentials_against_real_sandbox(tmp_path):
    world = build_world(tmp_path, seed_site=False, probe_through_fake=False)

    send(world.client, "set up my website", "wamid.sb.11")
    send(world.client, WP_URL, "wamid.sb.12")
    send(world.client, WP_USERNAME, "wamid.sb.13")
    send(world.client, "DefinitelyWrongPassword123", "wamid.sb.14")

    assert world.sender.sent[-1][1] == ONBOARD_INVALID_CREDENTIALS
    assert world.services.sites.sites_for_owner(OWNER) == []


def test_onboarding_unreachable_site_against_real_sandbox(tmp_path):
    world = build_world(tmp_path, seed_site=False, probe_through_fake=False)

    send(world.client, "set up my website", "wamid.sb.21")
    send(world.client, "https://wpbot-does-not-exist.invalid", "wamid.sb.22")
    send(world.client, WP_USERNAME, "wamid.sb.23")
    send(world.client, WP_APP_PASSWORD, "wamid.sb.24")

    assert world.sender.sent[-1][1] == ONBOARD_UNREACHABLE.format(
        url="https://wpbot-does-not-exist.invalid"
    )
    assert world.services.sites.sites_for_owner(OWNER) == []
