"""PRD §12 onboarding conversation, end to end against the real apps.

The owner goes from \"nothing set up\" to \"system active\" purely by
messaging WhatsApp: trigger -> site URL -> username -> application
password -> the B5 `/sites/onboard` validation endpoint. Success gets
example phrasings; every failure reason gets its own plain-language
message and the owner only re-sends the piece that was wrong.

The WordPress layer is the in-memory FakeWordPress (see
integration_harness); the real-sandbox variant of this conversation runs
where Docker exists (test_onboarding_sandbox.py).
"""

from __future__ import annotations

from integration_harness import OWNER, SITE, build_world, send
from wp_fake import FakeWordPress

from track_a.onboarding import (
    ONBOARD_CANCELLED,
    ONBOARD_INSUFFICIENT_PERMISSIONS,
    ONBOARD_INVALID_CREDENTIALS,
    ONBOARD_INVALID_URL,
    ONBOARD_NOT_WORDPRESS,
    ONBOARD_STEP_APP_PASSWORD,
    ONBOARD_STEP_URL,
    ONBOARD_STEP_USERNAME,
    ONBOARD_SUCCESS,
    ONBOARD_UNREACHABLE,
)


def complete_conversation(world, *, password: str = "app-pass") -> None:
    """Drive the full happy-path walkthrough, stopping after the password."""
    send(world.client, "set up my website", "wamid.ob.1")
    assert world.sender.sent[-1][1] == ONBOARD_STEP_URL

    send(world.client, SITE, "wamid.ob.2")
    assert world.sender.sent[-1][1] == ONBOARD_STEP_USERNAME.format(url=SITE)

    send(world.client, "editor", "wamid.ob.3")
    assert world.sender.sent[-1][1] == ONBOARD_STEP_APP_PASSWORD

    send(world.client, password, "wamid.ob.4")


def test_onboarding_success_end_to_end(tmp_path):
    world = build_world(tmp_path, seed_site=False)

    complete_conversation(world)

    # Success message: connection confirmed + example phrasings for the
    # three supported content types (job / announcement / business_info).
    last = world.sender.sent[-1][1]
    assert last == ONBOARD_SUCCESS.format(url=SITE)
    assert "Post a job" in last
    assert "announcement" in last
    assert "Change my hours to 9-6" in last

    # The site is actually onboarded on Track B (B5 persisted it).
    sites = world.services.sites.sites_for_owner(OWNER)
    assert len(sites) == 1
    assert sites[0].site_url == SITE
    assert sites[0].status == "active"

    # The walkthrough is over: the flow is inactive again.
    assert world.router.onboarding.is_active(OWNER) is False


def test_onboarding_invalid_url_is_rejected_locally(tmp_path):
    world = build_world(tmp_path, seed_site=False)

    send(world.client, "set up my website", "wamid.ob.1")
    send(world.client, "not a website", "wamid.ob.2")

    assert world.sender.sent[-1][1] == ONBOARD_INVALID_URL
    # Caught locally — Track B was never bothered with garbage.
    assert world.fake_wp.requests == []

    # Still awaiting the URL: a valid one proceeds.
    send(world.client, SITE, "wamid.ob.3")
    assert world.sender.sent[-1][1] == ONBOARD_STEP_USERNAME.format(url=SITE)


def test_onboarding_invalid_credentials_then_retry(tmp_path):
    fake = FakeWordPress(expected_auth=("editor", "correct-pass"))
    world = build_world(tmp_path, fake=fake, seed_site=False)

    complete_conversation(world, password="wrong-pass")
    assert world.sender.sent[-1][1] == ONBOARD_INVALID_CREDENTIALS
    # Only the password was wrong: URL + username kept, still active.
    assert world.router.onboarding.is_active(OWNER) is True

    # Re-sending just the password completes the walkthrough.
    send(world.client, "correct-pass", "wamid.ob.5")
    assert world.sender.sent[-1][1] == ONBOARD_SUCCESS.format(url=SITE)
    assert world.services.sites.sites_for_owner(OWNER)


def test_onboarding_insufficient_permissions(tmp_path):
    fake = FakeWordPress(expected_auth=("editor", "app-pass"), user_roles=("subscriber",))
    world = build_world(tmp_path, fake=fake, seed_site=False)

    complete_conversation(world)
    assert world.sender.sent[-1][1] == ONBOARD_INSUFFICIENT_PERMISSIONS
    # The flow asks for an Editor-level user again (back to the username
    # step — the next reply asks for the application password).
    send(world.client, "editor", "wamid.ob.5")
    assert world.sender.sent[-1][1] == ONBOARD_STEP_APP_PASSWORD
    # Nothing was persisted.
    assert world.services.sites.sites_for_owner(OWNER) == []


def test_onboarding_unreachable_site(tmp_path):
    fake = FakeWordPress(expected_auth=("editor", "app-pass"))
    fake.connect_error = True
    world = build_world(tmp_path, fake=fake, seed_site=False)

    complete_conversation(world)
    assert world.sender.sent[-1][1] == ONBOARD_UNREACHABLE.format(url=SITE)
    # Back to the URL step: a fresh URL starts the walkthrough again.
    send(world.client, SITE, "wamid.ob.5")
    assert world.sender.sent[-1][1] == ONBOARD_STEP_USERNAME.format(url=SITE)


def test_onboarding_not_wordpress_site(tmp_path):
    fake = FakeWordPress(expected_auth=("editor", "app-pass"))
    fake.inject_status = 404
    fake.inject_body = {"code": "rest_no_route", "message": "No route"}
    world = build_world(tmp_path, fake=fake, seed_site=False)

    complete_conversation(world)
    assert world.sender.sent[-1][1] == ONBOARD_NOT_WORDPRESS
    assert world.services.sites.sites_for_owner(OWNER) == []


def test_onboarding_cancel_aborts_cleanly(tmp_path):
    world = build_world(tmp_path, seed_site=False)

    send(world.client, "set up my website", "wamid.ob.1")
    send(world.client, "cancel", "wamid.ob.2")
    assert world.sender.sent[-1][1] == ONBOARD_CANCELLED
    assert world.router.onboarding.is_active(OWNER) is False

    # A fresh trigger restarts the walkthrough from step 1.
    send(world.client, "set up my website", "wamid.ob.3")
    assert world.sender.sent[-1][1] == ONBOARD_STEP_URL
