"""B2 allowlist gate: no intent reaches the WordPress client without
schema + allowlist validation first.

Covers the four required cases — enabled type + valid fields passes
through; disabled type rejected; unknown field rejected; malformed
intent rejected before any WordPress call — plus the dispatch paths
(create/update/delete/business_info/image) and result-object validity.
"""

import asyncio

import httpx
import pytest
from wp_fake import SITE, FakeWordPress

from shared_contract import CONTRACT_VERSION, validate_result
from track_b.allowlist import (
    PILOT_SITE_CONFIG,
    ContentTypeMapping,
    IntentNotAllowedError,
    SiteConfig,
    apply_intent,
    get_site_config,
    register_site_config,
    validate_intent_for_site,
)
from track_b.wordpress import WordPressClient

OWNER = "15551234567"


def run(coro):
    return asyncio.run(coro)


def make_intent(action="create", content_type="job", fields=None, confidence=0.95, **extra):
    intent = {
        "contract_version": CONTRACT_VERSION,
        "owner_id": OWNER,
        "action": action,
        "content_type": content_type,
        "fields": fields or {"title": "Barista", "description": "$18/hr"},
        "confidence": confidence,
    }
    intent.update(extra)
    return intent


@pytest.fixture()
def client_and_fake():
    fake = FakeWordPress()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return WordPressClient(SITE, "editor", "app-password", client=http), fake


def image_enabled_config() -> SiteConfig:
    """Pilot config with image (v1.5) switched on for one test."""
    mappings = dict(PILOT_SITE_CONFIG.mappings)
    mappings["image"] = ContentTypeMapping(
        content_type="image",
        enabled=True,
        image_slots=("homepage_banner", "logo", "gallery"),
        allowed_fields=("slot", "media_url", "media_base64"),
    )
    return SiteConfig(site_url=PILOT_SITE_CONFIG.site_url, mappings=mappings)


# ------------------------------------------------------- pass-through


def test_enabled_job_create_passes_through_to_client(client_and_fake):
    client, fake = client_and_fake
    intent = make_intent("create", "job", {"title": "Barista", "description": "$18/hr"})

    result = run(apply_intent(intent, PILOT_SITE_CONFIG, client))

    assert result["status"] == "success"
    validate_result(result)
    assert result["after"]["title"] == "Barista"
    assert result["after"]["status"] == "publish"
    assert result["live_url"] == f"{SITE}/?p=1"
    assert any(r.url.path == "/wp-json/wp/v2/posts" and r.method == "POST" for r in fake.requests)


def test_enabled_business_info_update_passes_through(client_and_fake):
    client, fake = client_and_fake
    fake.option = {"hours": "Mon-Fri 9-6"}
    intent = make_intent("update", "business_info", {"hours": "Mon-Fri 9-5"})

    result = run(apply_intent(intent, PILOT_SITE_CONFIG, client))

    assert result["status"] == "success"
    assert result["before"] == {"hours": "Mon-Fri 9-6"}
    assert result["after"] == {"hours": "Mon-Fri 9-5"}


def test_update_job_resolves_post_by_title(client_and_fake):
    client, _ = client_and_fake
    run(client.create_post("job", {"title": "Barista", "description": "$18/hr"}))
    intent = make_intent("update", "job", {"title": "Barista", "description": "$20/hr"})

    result = run(apply_intent(intent, PILOT_SITE_CONFIG, client))

    assert result["status"] == "success"
    assert result["before"]["content"] == "$18/hr"
    assert result["after"]["content"] == "$20/hr"


def test_update_job_title_not_found_returns_failed(client_and_fake):
    client, _ = client_and_fake
    intent = make_intent("update", "job", {"title": "Nobody", "description": "x"})

    result = run(apply_intent(intent, PILOT_SITE_CONFIG, client))

    assert result["status"] == "failed"
    validate_result(result)
    assert "was found" in result["error_message"]
    assert result["live_url"] is None


# ------------------------------------------------------- rejections


def test_disabled_content_type_rejected_before_any_write(client_and_fake):
    client, fake = client_and_fake
    intent = make_intent(
        "update", "image", {"slot": "logo", "media_base64": "AA=="}
    )  # image is disabled in the pilot config

    result = run(apply_intent(intent, PILOT_SITE_CONFIG, client))

    assert result["status"] == "failed"
    validate_result(result)
    assert "not enabled" in result["error_message"]
    assert fake.requests == []  # NO WordPress call happened


def test_unknown_field_rejected(client_and_fake):
    client, fake = client_and_fake
    intent = make_intent(
        "create",
        "job",
        {"title": "Barista", "description": "$18/hr", "prices": "$18"},
    )

    result = run(apply_intent(intent, PILOT_SITE_CONFIG, client))

    assert result["status"] == "failed"
    assert "prices" in result["error_message"]
    assert "not allowed" in result["error_message"]
    assert fake.requests == []


def test_malformed_intent_rejected_before_any_wordpress_call(client_and_fake):
    client, fake = client_and_fake
    malformed = make_intent()
    del malformed["confidence"]  # fails intent.schema.json

    result = run(apply_intent(malformed, PILOT_SITE_CONFIG, client))

    assert result["status"] == "failed"
    validate_result(result)
    assert "confidence" in result["error_message"]
    assert fake.requests == []  # the guardrail: no write attempt at all


def test_bad_action_rejected_by_schema(client_and_fake):
    client, fake = client_and_fake
    intent = make_intent(action="publish", content_type="job")

    result = run(apply_intent(intent, PILOT_SITE_CONFIG, client))

    assert result["status"] == "failed"
    assert fake.requests == []


def test_unknown_site_config_raises():
    with pytest.raises(Exception) as exc:
        get_site_config("https://not-registered.example")
    assert "no site configuration" in str(exc.value)


# ------------------------------------------------------- image (v1.5, opt-in)


def test_image_with_base64_passes_through_when_enabled(client_and_fake):
    client, fake = client_and_fake
    fake.has_muplugin = True
    intent = make_intent(
        "update",
        "image",
        {"slot": "homepage_banner", "media_base64": "aGVsbG8="},  # "hello"
    )

    result = run(apply_intent(intent, image_enabled_config(), client))

    assert result["status"] == "success"
    assert result["after"]["image_slot"] == "homepage_banner"
    # Uploaded (no delete of the previous image), slot pointed at new URL.
    assert any(r.url.path == "/wp-json/wp/v2/media" and r.method == "POST" for r in fake.requests)
    assert not any(r.method == "DELETE" for r in fake.requests)
    assert fake.option["image:homepage_banner"] == f"{SITE}/wp-content/uploads/1.jpg"


def test_image_with_media_url_rejected_clear_error(client_and_fake):
    client, fake = client_and_fake
    intent = make_intent("update", "image", {"slot": "logo", "media_url": "https://x/y.jpg"})
    result = run(apply_intent(intent, image_enabled_config(), client))
    assert result["status"] == "failed"
    assert "media_base64" in result["error_message"]
    assert fake.requests == []


# ------------------------------------------------------- pure validator


def test_validate_intent_for_site_raises_typed_errors():
    valid = make_intent()
    validate_intent_for_site(valid, PILOT_SITE_CONFIG)  # no raise

    # image must be schema-valid (slot + media) to reach the enablement check.
    with pytest.raises(IntentNotAllowedError):
        validate_intent_for_site(
            make_intent("update", "image", {"slot": "logo", "media_base64": "AA=="}),
            PILOT_SITE_CONFIG,
        )
    # A schema-valid field the SITE disallows is what the field allowlist
    # guards (a field outside the schema is already caught by validate_intent).
    no_remote = SiteConfig(
        site_url=PILOT_SITE_CONFIG.site_url,
        mappings={
            "job": ContentTypeMapping(
                content_type="job",
                allowed_fields=("title", "description"),
            )
        },
    )
    with pytest.raises(IntentNotAllowedError) as exc:
        validate_intent_for_site(
            make_intent("create", "job", {"title": "X", "description": "y", "remote": True}),
            no_remote,
        )
    assert "remote" in str(exc.value)


def test_register_site_config_allows_override():
    custom = SiteConfig(
        site_url="https://custom.example",
        mappings={
            "announcement": ContentTypeMapping(
                content_type="announcement",
                allowed_fields=("title", "body"),
            )
        },
    )
    register_site_config(custom)
    assert get_site_config("https://custom.example") is custom
