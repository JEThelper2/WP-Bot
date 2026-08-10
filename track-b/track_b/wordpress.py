"""WordPress REST API client (Track B, B1).

Talks to a WordPress site's REST API using an **application password**
(Basic auth) tied to a dedicated Editor-level user — never an
Administrator and never the site owner's normal password. Credentials
live encrypted at rest (see `track_b.secrets`) and are scrubbed from
every error message this module raises.

Operations (all return a `ChangeRecord` with enough `before`/`after`
state to populate a contract result object):

- `create_post(content_type, fields)` — job/announcement. Uses the custom
  post type `jobs` when the site registers one; otherwise falls back to a
  standard post assigned to the `jobs`/`announcements` category (PRD §6).
- `update_post(post_id, fields)` — partial update; `before` is fetched
  first so the diff is real.
- `delete_post(post_id)` — trashes the post (recoverable, and it leaves
  the site immediately).
- `update_site_option(fields)` — the business_info singleton. WordPress's
  core `settings` endpoint requires `manage_options` (admin), which
  violates the Editor-only guardrail, so this uses the custom
  `wpbot/v1/business-info` REST route provided by the bundled mu-plugin
  (see `wp-sandbox/`). A clear error explains the requirement when the
  route is missing.
- `upload_and_replace_image(slot, media)` — **v1.5, optional for the
  MVP**. Uploads to the Media Library, never deletes the previous image,
  and stores the new attachment URL into the slot (per the allowlist in
  `track_b.config`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import IMAGE_SLOT_ALLOWLIST
from .secrets import redact

logger = logging.getLogger("track_b.wordpress")

# Which REST post type a content_type maps to, when the site has no custom
# post type. Standard posts + category is the PRD §6 default.
_CONTENT_TYPE_CATEGORY = {"job": "jobs", "announcement": "announcements"}


class WordPressError(Exception):
    """A clear, credential-free error surfaced to the caller.

    `message` is always scrubbed (see `redact`) so application passwords
    can never reach a log or the result object's error_message.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        site_url: str | None = None,
    ) -> None:
        super().__init__(redact(message))
        self.status_code = status_code
        self.site_url = site_url


@dataclass
class ChangeRecord:
    """Before/after state for one write, ready for a result object."""

    before: dict[str, Any] | None
    after: dict[str, Any] | None
    post_id: int | None = None
    live_url: str | None = None


class WordPressClient:
    def __init__(
        self,
        site_url: str,
        username: str,
        app_password: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = site_url.rstrip("/")
        self._auth = (username, app_password)
        self._client = client or httpx.AsyncClient(
            auth=self._auth, timeout=timeout
        )

    # ------------------------------------------------------------ posts

    async def create_post(
        self, content_type: str, fields: dict[str, Any]
    ) -> ChangeRecord:
        """Create a job/announcement post; return before/after state."""
        if content_type not in _CONTENT_TYPE_CATEGORY:
            raise WordPressError(
                f"create_post does not support content_type {content_type!r}; "
                "expected 'job' or 'announcement'"
            )
        post_type = await self._resolve_post_type(content_type)
        # Category assignment is for the standard-post fallback; a custom
        # 'jobs' post type needs no category (PRD §6).
        categories = None
        if post_type == "posts":
            categories = [await self._ensure_category(content_type)]
        payload = self._post_payload(content_type, fields, categories)

        created = await self._request(
            "POST", f"/wp-json/wp/v2/{post_type}", json=payload
        )
        created = created.json()
        return ChangeRecord(
            before=None,
            after=self._post_state(created),
            post_id=created["id"],
            live_url=created.get("link"),
        )

    async def update_post(
        self,
        post_id: int,
        fields: dict[str, Any],
        content_type: str | None = None,
    ) -> ChangeRecord:
        """Partial update; `before` is the current on-site state."""
        post_type, current = await self._get_post(post_id, content_type)
        payload = self._post_payload(
            content_type or current.get("_wpbot_content_type", "post"),
            fields,
            categories=None,
            partial=True,
        )
        updated = await self._request(
            "PUT", f"/wp-json/wp/v2/{post_type}/{post_id}", json=payload
        )
        updated = updated.json()
        return ChangeRecord(
            before=self._post_state(current),
            after=self._post_state(updated),
            post_id=post_id,
            live_url=updated.get("link"),
        )

    async def delete_post(
        self, post_id: int, content_type: str | None = None
    ) -> ChangeRecord:
        """Trash the post (recoverable); `before` is its final live state."""
        post_type, current = await self._get_post(post_id, content_type)
        # force=false (default) moves it to the trash — reversible, and the
        # post leaves the live site immediately.
        await self._request(
            "DELETE", f"/wp-json/wp/v2/{post_type}/{post_id}"
        )
        return ChangeRecord(
            before=self._post_state(current),
            after={"deleted": True, "post_id": post_id, "status": "trash"},
            post_id=post_id,
            live_url=None,
        )

    async def _get_post(
        self, post_id: int, content_type: str | None
    ) -> tuple[str, dict[str, Any]]:
        """Fetch a post, probing the standard route first, then a custom
        post type. Returns (rest_post_type, post)."""
        if content_type in _CONTENT_TYPE_CATEGORY:
            post_type = await self._resolve_post_type(content_type)
            resp = await self._request(
                "GET", f"/wp-json/wp/v2/{post_type}/{post_id}", allow_404=True
            )
            if resp is not None:
                post = resp.json()
                post["_wpbot_content_type"] = content_type
                return post_type, post
            raise WordPressError(
                f"post {post_id} not found on {self.base_url}",
                status_code=404,
                site_url=self.base_url,
            )

        resp = await self._request(
            "GET", f"/wp-json/wp/v2/posts/{post_id}", allow_404=True
        )
        if resp is not None:
            return "posts", resp.json()
        resp = await self._request(
            "GET", f"/wp-json/wp/v2/jobs/{post_id}", allow_404=True
        )
        if resp is not None:
            post = resp.json()
            post["_wpbot_content_type"] = "job"
            return "jobs", post
        raise WordPressError(
            f"post {post_id} not found on {self.base_url}",
            status_code=404,
            site_url=self.base_url,
        )

    async def _resolve_post_type(self, content_type: str) -> str:
        """'jobs' custom post type if the site registers it, else 'posts'."""
        if content_type != "job":
            return "posts"  # announcements are always standard posts
        resp = await self._request(
            "GET", "/wp-json/wp/v2/types", allow_404=True
        )
        if resp is not None and "jobs" in resp.json():
            return "jobs"
        return "posts"

    async def _ensure_category(self, content_type: str) -> int:
        """Get-or-create the 'jobs'/'announcements' category; return its id."""
        name = _CONTENT_TYPE_CATEGORY[content_type]
        resp = await self._request(
            "GET", f"/wp-json/wp/v2/categories?search={name}"
        )
        for cat in resp.json():
            if cat.get("name", "").lower() == name:
                return int(cat["id"])
        created = await self._request(
            "POST", "/wp-json/wp/v2/categories", json={"name": name}
        )
        return int(created.json()["id"])

    @staticmethod
    def _post_payload(
        content_type: str,
        fields: dict[str, Any],
        categories: list[int] | None,
        *,
        partial: bool = False,
    ) -> dict[str, Any]:
        """Map contract fields onto WordPress post fields."""
        payload: dict[str, Any] = {}
        if "title" in fields:
            payload["title"] = str(fields["title"])
        if content_type == "job" and fields.get("description") is not None:
            payload["content"] = str(fields["description"])
        elif content_type == "announcement" and fields.get("body") is not None:
            payload["content"] = str(fields["body"])
        elif content_type is None:
            # Content type unknown (probed post): accept either shape.
            body = fields.get("description") or fields.get("body")
            if body is not None:
                payload["content"] = str(body)

        # Structured details for job extras and announcement expiry.
        details: list[str] = []
        if content_type == "job":
            if fields.get("location"):
                details.append(f"Location: {fields['location']}")
            if fields.get("remote") is not None:
                details.append("Remote: Yes" if fields["remote"] else "Remote: No")
        if content_type == "announcement" and fields.get("expires_at"):
            details.append(f"Expires: {fields['expires_at']}")
        if details and not partial:
            content = str(payload.get("content") or "")
            payload["content"] = (
                f"{content}\n\nDetails:\n" + "\n".join(details)
            ).strip()

        if categories is not None:
            payload["categories"] = categories
        if not partial:
            payload["status"] = "publish"
        return payload

    @staticmethod
    def _post_state(post: dict[str, Any]) -> dict[str, Any]:
        """Field-level state for before/after — raw values when available."""
        return {
            "post_id": post.get("id"),
            "title": post.get("title", {}).get("raw") or post.get("title", {}).get("rendered"),
            "content": post.get("content", {}).get("raw") or post.get("content", {}).get("rendered"),
            "status": post.get("status"),
            "categories": post.get("categories"),
            "link": post.get("link"),
        }

    # ---------------------------------------------------- business info

    async def update_site_option(self, fields: dict[str, Any]) -> ChangeRecord:
        """Update the business_info singleton via the wpbot custom route.

        WordPress's core `settings` endpoint requires `manage_options`
        (admin) — the Editor-only guardrail forbids that — so this uses
        the mu-plugin's `wpbot/v1/business-info` route, which is gated on
        `edit_posts` and writes only allowlisted option keys.
        """
        before = await self._get_business_info()
        resp = await self._request(
            "POST",
            "/wp-json/wpbot/v1/business-info",
            json={"fields": fields},
        )
        after = resp.json().get("value", {})
        return ChangeRecord(before=before or {}, after=after)

    async def _get_business_info(self) -> dict[str, Any] | None:
        resp = await self._request(
            "GET", "/wp-json/wpbot/v1/business-info", allow_404=True
        )
        if resp is None:
            raise WordPressError(
                f"site {self.base_url} does not expose the wpbot/v1/business-info "
                "REST route. Install the bundled mu-plugin (wp-sandbox/mu-plugins/"
                "wpbot-business-info.php) — business_info updates cannot use the "
                "core settings endpoint because that requires admin rights, which "
                "the Editor-only security guardrail forbids.",
                status_code=404,
                site_url=self.base_url,
            )
        return resp.json().get("value") or {}

    # --------------------------------------------------------- images (v1.5)

    async def upload_and_replace_image(
        self, slot: str, media: dict[str, Any]
    ) -> ChangeRecord:
        """v1.5 (optional): upload to Media Library and swap the slot.

        Never deletes the previous image — it stays in the Media Library.
        The slot must be in the allowlist (see track_b.config); the new
        attachment URL is stored under the slot's option key via the
        business-info route.
        """
        target = IMAGE_SLOT_ALLOWLIST.get(slot)
        if target is None:
            raise WordPressError(
                f"image slot {slot!r} is not in the allowlist; "
                f"allowed slots: {', '.join(sorted(IMAGE_SLOT_ALLOWLIST))}"
            )
        if not media.get("content"):
            raise WordPressError("upload_and_replace_image requires media content")

        filename = media.get("filename", "wpbot-upload")
        mime_type = media.get("mime_type", "application/octet-stream")
        resp = await self._request(
            "POST",
            "/wp-json/wp/v2/media",
            files={
                "file": (
                    filename,
                    media["content"],
                    mime_type,
                )
            },
            params={"post": 0},
        )
        attachment = resp.json()
        url = attachment.get("source_url") or attachment.get("link")

        # Swap the slot to the new URL (previous image untouched).
        swap = await self.update_site_option({target: url})
        return ChangeRecord(
            before=swap.before,
            after={**swap.after, "image_slot": slot, "media_id": attachment.get("id")},
            live_url=url,
        )

    # ------------------------------------------------------------ plumbing

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_404: bool = False,
        **kwargs: Any,
    ) -> httpx.Response | None:
        url = f"{self.base_url}{path}"
        try:
            resp = await self._client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise WordPressError(
                f"could not reach WordPress at {self.base_url} ({type(exc).__name__}); "
                "check the site URL and network access",
                site_url=self.base_url,
            ) from exc
        except httpx.HTTPError as exc:
            raise WordPressError(
                f"request to {self.base_url} failed: {redact(str(exc))}",
                site_url=self.base_url,
            ) from exc

        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code in (401, 403):
            raise WordPressError(
                "WordPress rejected the credentials (HTTP "
                f"{resp.status_code}). Check the application password and that "
                "the user has the required role (Editor for posts/categories, "
                "see the setup docs).",
                status_code=resp.status_code,
                site_url=self.base_url,
            )
        if resp.status_code >= 400:
            code = ""
            message = ""
            try:
                body = resp.json()
                code = body.get("code", "")
                message = body.get("message", "")
            except ValueError:
                pass
            raise WordPressError(
                f"WordPress API error on {self.base_url}{path}: "
                f"{code} {message}".strip()
                or f"HTTP {resp.status_code}",
                status_code=resp.status_code,
                site_url=self.base_url,
            )
        return resp
