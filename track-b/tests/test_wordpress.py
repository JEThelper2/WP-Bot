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

from track_b.wordpress import ChangeRecord, WordPressClient, WordPressError

SITE = "https://wp.example.com"
USER = "editor"
APP_PASSWORD = "SuperSecretAppPass123"


def run(coro):
    return asyncio.run(coro)


def wp_post(post_id, title, content, status="publish", categories=(1,)):
    return {
        "id": post_id,
        "title": {"raw": title, "rendered": title},
        "content": {"raw": content, "rendered": content},
        "status": status,
        "categories": list(categories),
        "link": f"{SITE}/?p={post_id}",
    }


class FakeWordPress:
    """A minimal in-memory WordPress REST API for tests."""

    def __init__(self, *, has_jobs_cpt=False, has_muplugin=True):
        self.posts: dict[int, dict] = {}
        self.next_id = 1
        self.categories = {"jobs": 10, "announcements": 11}
        self.option: dict = {}
        self.media: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.has_jobs_cpt = has_jobs_cpt
        self.has_muplugin = has_muplugin
        self.inject_status: int | None = None  # e.g. 401/500 for failure tests
        self.inject_body: dict | None = None
        self.connect_error = False

    # -- handlers ---------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.inject_status:
            return httpx.Response(
                self.inject_status,
                json=self.inject_body or {"code": "injected", "message": "injected"},
            )
        if self.connect_error:
            raise httpx.ConnectError("connection refused", request=request)

        method = request.method
        path = request.url.path
        if path == "/wp-json/wp/v2/types":
            types = {"post": {"rest_base": "posts"}}
            if self.has_jobs_cpt:
                types["jobs"] = {"rest_base": "jobs"}
            return httpx.Response(200, json=types)
        if path == "/wp-json/wp/v2/categories":
            if method == "GET":
                name = request.url.params.get("search", "").lower()
                cats = [
                    {"id": cid, "name": cname}
                    for cname, cid in self.categories.items()
                    if cname == name
                ]
                return httpx.Response(200, json=cats)
            body = json.loads(request.content)
            name = body["name"]
            if name not in self.categories:
                self.categories[name] = max(self.categories.values()) + 1
            return httpx.Response(200, json={"id": self.categories[name], "name": name})
        if path == "/wp-json/wpbot/v1/business-info":
            if not self.has_muplugin:
                return httpx.Response(404, json={"code": "rest_no_route", "message": "No route"})
            if method == "GET":
                return httpx.Response(200, json={"value": self.option})
            body = json.loads(request.content)
            for key, value in body.get("fields", {}).items():
                self.option[key] = value
            return httpx.Response(200, json={"value": self.option})
        if path == "/wp-json/wp/v2/media":
            attachment = {
                "id": len(self.media) + 1,
                "source_url": f"{SITE}/wp-content/uploads/{len(self.media) + 1}.jpg",
            }
            self.media.append(attachment)
            return httpx.Response(201, json=attachment)

        # posts / jobs CRUD
        segments = [s for s in path.split("/") if s]
        if segments[-1] in ("posts", "jobs"):
            post_type = segments[-1]
            collection = True
        elif len(segments) >= 5 and segments[-2] in ("posts", "jobs"):
            post_type = segments[-2]
            collection = False
        else:
            return httpx.Response(404, json={"code": "rest_no_route", "message": "No route"})
        if method == "POST":
            body = json.loads(request.content)
            post_id = self.next_id
            self.next_id += 1
            post = wp_post(
                post_id,
                body.get("title", ""),
                body.get("content", ""),
                body.get("status", "publish"),
                body.get("categories", []),
            )
            self.posts[post_id] = post
            return httpx.Response(201, json=post)
        if collection:
            return httpx.Response(404, json={"code": "rest_post_invalid_id", "message": "Invalid post ID."})
        post_id = int(segments[-1])
        post = self.posts.get(post_id)
        if post is None:
            return httpx.Response(404, json={"code": "rest_post_invalid_id", "message": "Invalid post ID."})
        if method == "GET":
            return httpx.Response(200, json=post)
        if method == "PUT":
            body = json.loads(request.content)
            for key, value in body.items():
                if key == "title":
                    post["title"] = {"raw": value, "rendered": value}
                elif key == "content":
                    post["content"] = {"raw": value, "rendered": value}
                else:
                    post[key] = value
            return httpx.Response(200, json=post)
        if method == "DELETE":
            post["status"] = "trash"
            return httpx.Response(200, json=post)
        return httpx.Response(405, json={"code": "rest_no_route", "message": "No route"})


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
        wp_client.create_post(
            "announcement", {"title": "Holiday Hours", "body": "Closed Monday"}
        )
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
            {"title": "Cashier", "description": "evenings", "location": "Downtown", "remote": False},
        )
    )
    assert record.after["content"] == (
        "evenings\n\nDetails:\nLocation: Downtown\nRemote: No"
    )


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
