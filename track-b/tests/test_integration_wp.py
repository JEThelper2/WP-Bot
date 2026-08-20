"""Integration tests against a REAL WordPress install (the Docker sandbox).

These are skipped unless a sandbox is running. Spin one up with:

    docker compose -f track-b/wp-sandbox/docker-compose.yml up -d
    docker compose -f track-b/wp-sandbox/docker-compose.yml logs -f setup

then point the suite at it (the password lands in
`track-b/wp-sandbox/_output/app-password.txt`):

    WPBOT_WP_TEST_URL=http://localhost:8090 \
    WPBOT_WP_TEST_USERNAME=editor \
    WPBOT_WP_TEST_APP_PASSWORD=$(cat track-b/wp-sandbox/_output/app-password.txt) \
    pytest track-b/tests/test_integration_wp.py -v

The sandbox user is EDITOR with an application password — never admin.
"""

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from track_b.wordpress import WordPressClient, WordPressError

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


def run(coro):
    """Run an async coroutine in a fresh event loop.

    Each call creates a new event loop, so we must NOT share an
    httpx.AsyncClient across calls (its connections are bound to
    the loop that created them). Instead, the client fixture below
    lets WordPressClient create its own client per call.
    """
    return asyncio.run(coro)


@pytest.fixture()
def client() -> WordPressClient:
    # Do NOT inject a shared httpx.AsyncClient — each run() call creates
    # a fresh event loop and connections from a prior loop are invalid.
    # WordPressClient will create its own client when none is provided.
    return WordPressClient(WP_URL, WP_USERNAME, WP_APP_PASSWORD)


@pytest.fixture()
def cleanup() -> list[tuple[int, str | None]]:
    created: list[tuple[int, str | None]] = []

    yield created

    # Each cleanup call needs its own WordPressClient (fresh event loop).
    for post_id, content_type in created:
        try:
            c = WordPressClient(WP_URL, WP_USERNAME, WP_APP_PASSWORD)
            run(c.delete_post(post_id, content_type=content_type))
        except Exception:
            pass


def test_full_lifecycle_create_update_delete(cleanup) -> None:
    client = WordPressClient(WP_URL, WP_USERNAME, WP_APP_PASSWORD)
    # --- create ---------------------------------------------------------
    created = run(
        client.create_post(
            "job",
            {"title": "IT Job Cashier", "description": "evenings", "location": "Downtown"},
        )
    )
    assert created.before is None
    assert created.after["title"] == "IT Job Cashier"
    assert "evenings" in created.after["content"]
    assert created.after["status"] == "publish"
    assert created.live_url and created.live_url.startswith(WP_URL)
    cleanup.append((created.post_id, "job"))

    # --- update (fresh client for new event loop) ------------------------
    client2 = WordPressClient(WP_URL, WP_USERNAME, WP_APP_PASSWORD)
    updated = run(
        client2.update_post(
            created.post_id,
            {"description": "now $20/hr"},
            content_type="job",
        )
    )
    assert updated.before["title"] == "IT Job Cashier"
    assert updated.before["content"] != updated.after["content"]
    assert "now $20/hr" in updated.after["content"]
    assert updated.after["title"] == "IT Job Cashier"  # untouched by partial update

    # --- delete (fresh client for new event loop) ------------------------
    client3 = WordPressClient(WP_URL, WP_USERNAME, WP_APP_PASSWORD)
    deleted = run(client3.delete_post(created.post_id, content_type="job"))
    assert deleted.before["post_id"] == created.post_id
    assert deleted.after["deleted"] is True
    assert deleted.after["status"] == "trash"


def test_business_info_update_via_muplugin() -> None:
    # First call
    client1 = WordPressClient(WP_URL, WP_USERNAME, WP_APP_PASSWORD)
    first = run(client1.update_site_option({"hours": "Mon-Fri 9-6"}))
    assert first.after["hours"] == "Mon-Fri 9-6"

    # Second call (fresh client for new event loop)
    client2 = WordPressClient(WP_URL, WP_USERNAME, WP_APP_PASSWORD)
    second = run(client2.update_site_option({"hours": "Mon-Fri 9-5", "phone": "(555) 123-4567"}))
    assert second.before["hours"] == "Mon-Fri 9-6"  # real prior state
    assert second.after["hours"] == "Mon-Fri 9-5"
    assert second.after["phone"] == "(555) 123-4567"


def test_invalid_application_password_surfaces_clear_error() -> None:
    bad = WordPressClient(WP_URL, WP_USERNAME, "DefinitelyWrongPassword123")
    with pytest.raises(WordPressError) as exc:
        run(bad.create_post("job", {"title": "X", "description": "y"}))
    assert "rejected the credentials" in str(exc.value)
    assert "DefinitelyWrongPassword123" not in str(exc.value)
