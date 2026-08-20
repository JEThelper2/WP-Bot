"""WordPress client unit tests against a mocked WordPress REST API.

Covers the required operations (create/update/delete post, business_info
option, v1.5 image), the before/after capture, the standard-post +
category fallback vs the 'jobs' custom post type, and the failure cases
(auth, unreachable site, missing route) — asserting every error is clear
and that the application password never leaks into an exception message.
"""

import asyncio
import json

import httpx
import pytest
from wp_fake import SITE, FakeWordPress

from track_b.wordpress import ChangeRecord, WordPressClient, WordPressError

USER = "editor"
APP_PASSWORD = "SuperSecretAppPass123"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def make_client() -> tuple[WordPressClient, FakeWordPress]:
    fake = FakeWordPress()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return WordPressClient(SITE, USER, APP_PASSWORD, client=http), fake


# ------------------------------------------------------------ create


def test_create_job_uses_custom_post_type_when_present(make_client):
    wp_client, fake = make_client
    fake.has_jobs_cpt = True
    record = run(wp_client.create_post("job", {"title": "Barista", "description": "$18/hr"}))

    assert isinstance(record, ChangeRecord)
    assert record.before is None  # nothing existed before a create
    assert record.after["title"] == "Barista"
    assert record.after["content"] == "$18/hr"
    assert record.after["status"] == "publish"
    assert record.live_url == f"{SITE}/?p=1"
    # Went to the custom post type, and no category was created/assigned.
    assert any(r.url.path == "/wp-json/wp/v2/jobs" for r in fake.requests)
    assert not any("/categories" in r.url.path for r in fake.requests)


def test_create_announcement_falls_back_to_standard_post_with_category(make_client):
    wp_client, fake = make_client  # no jobs CPT
    fake.categories.pop("announcements", None)  # category must be created
    record = run(
        wp_client.create_post("announcement", {"title": "Holiday Hours", "body": "Closed Monday"})
    )

    assert record.after["title"] == "Holiday Hours"
    assert record.after["content"] == "Closed Monday"
    # Standard posts route, category created and assigned.
    assert any(r.url.path == "/wp-json/wp/v2/posts" for r in fake.requests)
    assert any(r.method == "POST" and "/categories" in r.url.path for r in fake.requests)
    assert record.after["categories"] == [11]


def test_create_job_appends_location_and_remote_details(make_client):
    wp_client, _ = make_client
    record = run(
        wp_client.create_post(
            "job",
            {
                "title": "Cashier",
                "description": "evenings",
                "location": "Downtown",
                "remote": False,
            },
        )
    )
    assert record.after["content"] == ("evenings\n\nDetails:\nLocation: Downtown\nRemote: No")


# ------------------------------------------------------------ update / delete


def test_update_post_captures_real_before_and_after(make_client):
    wp_client, fake = make_client
    record = run(wp_client.create_post("job", {"title": "Barista", "description": "$18/hr"}))
    post_id = record.post_id

    updated = run(wp_client.update_post(post_id, {"description": "$20/hr"}, content_type="job"))

    assert updated.before["title"] == "Barista"
    assert updated.before["content"] == "$18/hr"
    assert updated.after["title"] == "Barista"  # unchanged
    assert updated.after["content"] == "$20/hr"
    # Partial: only content changed; the request carried no title.
    put = next(r for r in fake.requests if r.method == "PUT")
    body = json.loads(put.content)
    assert "title" not in body
    assert body["content"] == "$20/hr"


def test_delete_post_trashes_and_captures_before(make_client):
    wp_client, _ = make_client
    record = run(wp_client.create_post("announcement", {"title": "Sale", "body": "20% off"}))
    post_id = record.post_id

    deleted = run(wp_client.delete_post(post_id, content_type="announcement"))
    assert deleted.before["title"] == "Sale"
    assert deleted.after == {"deleted": True, "post_id": post_id, "status": "trash"}
    assert deleted.live_url is None


def test_update_missing_post_raises_clear_error(make_client):
    wp_client, _ = make_client
    with pytest.raises(WordPressError) as exc:
        run(wp_client.update_post(999, {"title": "X"}, content_type="job"))
    assert "not found" in str(exc.value)


def test_find_post_by_title(make_client):
    wp_client, _ = make_client
    run(wp_client.create_post("job", {"title": "Barista", "description": "$18/hr"}))
    run(wp_client.create_post("job", {"title": "Cashier", "description": "evenings"}))

    assert run(wp_client.find_post_by_title("job", "cashier")) == 2  # case-insensitive
    assert run(wp_client.find_post_by_title("job", "Nobody")) is None


# ------------------------------------------------------------ business info


def test_business_info_update_via_muplugin_route(make_client):
    wp_client, fake = make_client
    fake.option = {"hours": "Mon-Fri 9-6"}

    record = run(wp_client.update_site_option({"hours": "Mon-Fri 9-5", "phone": "(555) 1"}))

    assert record.before == {"hours": "Mon-Fri 9-6"}  # real prior state
    assert record.after["hours"] == "Mon-Fri 9-5"
    assert record.after["phone"] == "(555) 1"
    assert fake.option["hours"] == "Mon-Fri 9-5"


def test_business_info_without_muplugin_raises_actionable_error(make_client):
    wp_client, fake = make_client
    fake.has_muplugin = False
    with pytest.raises(WordPressError) as exc:
        run(wp_client.update_site_option({"hours": "9-5"}))
    message = str(exc.value)
    assert "wpbot/v1/business-info" in message
    assert "mu-plugin" in message
    assert "Editor-only" in message or "admin" in message.lower()


# ------------------------------------------------------------ images (v1.5)


def test_upload_and_replace_image_uploads_without_deleting_previous(make_client):
    wp_client, fake = make_client
    media = {"content": b"\xff\xd8fakejpeg", "filename": "banner.jpg", "mime_type": "image/jpeg"}

    record = run(wp_client.upload_and_replace_image("homepage_banner", media))

    assert record.live_url == f"{SITE}/wp-content/uploads/1.jpg"
    assert record.after["image_slot"] == "homepage_banner"
    assert record.after["media_id"] == 1
    # Uploaded to the Media Library; NO delete of the previous image.
    assert any(r.url.path == "/wp-json/wp/v2/media" for r in fake.requests)
    assert not any(r.method == "DELETE" for r in fake.requests)
    # The slot's option key now points at the new URL.
    assert fake.option["image:homepage_banner"] == f"{SITE}/wp-content/uploads/1.jpg"


def test_image_slot_not_in_allowlist_is_rejected(make_client):
    wp_client, _ = make_client
    with pytest.raises(WordPressError) as exc:
        run(wp_client.upload_and_replace_image("footer_logo", {"content": b"x"}))
    assert "not in the allowlist" in str(exc.value)


# ------------------------------------------------------------ failures


def test_auth_failure_raises_clear_error_and_never_leaks_password(make_client):
    wp_client, fake = make_client
    fake.inject_status = 401
    with pytest.raises(WordPressError) as exc:
        run(wp_client.create_post("job", {"title": "X", "description": "y"}))
    assert "rejected the credentials" in str(exc.value)
    assert "application password" in str(exc.value)
    assert APP_PASSWORD not in str(exc.value)


def test_unreachable_site_raises_clear_error(make_client):
    wp_client, fake = make_client
    fake.connect_error = True
    with pytest.raises(WordPressError) as exc:
        run(wp_client.create_post("job", {"title": "X", "description": "y"}))
    assert "could not reach" in str(exc.value)
    assert APP_PASSWORD not in str(exc.value)


def test_wp_error_body_is_surfaced(make_client):
    wp_client, fake = make_client
    fake.inject_status = 500
    fake.inject_body = {"code": "db_error", "message": "database connection failed"}
    with pytest.raises(WordPressError) as exc:
        run(wp_client.create_post("job", {"title": "X", "description": "y"}))
    assert "db_error" in str(exc.value)
    assert "database connection failed" in str(exc.value)
    assert APP_PASSWORD not in str(exc.value)


def test_unsupported_content_type_rejected_before_any_http(make_client):
    wp_client, fake = make_client
    with pytest.raises(WordPressError):
        run(wp_client.create_post("business_info", {"hours": "9-5"}))
    assert fake.requests == []
