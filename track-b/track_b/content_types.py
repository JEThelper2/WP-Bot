"""Content type handler registry (Open/Closed Principle).

New content types are added by:
1. Creating a handler class that implements `ContentTypeHandler`
2. Registering it with `register_handler()`

No existing code needs modification — this is the OCP seam for content types.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from .changelog import ChangeLog, ChangeRow
from .wordpress import WordPressClient, WordPressError

logger = logging.getLogger("track_b.content_types")


class ContentTypeHandler(Protocol):
    """Interface for content type handlers.

    Each content type (job, announcement, business_info, image, etc.)
    implements this interface. Adding a new content type means adding
    a new class that satisfies this protocol — no existing code changes.
    """

    @property
    def content_type(self) -> str: ...

    @property
    def required_on_create(self) -> tuple[str, ...]: ...

    @property
    def field_questions(self) -> dict[str, str]: ...

    async def apply(
        self,
        intent: dict[str, Any],
        client: WordPressClient,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, ContentTypeHandler] = {}


def register_handler(handler: ContentTypeHandler) -> None:
    """Register a content type handler. Called at module load time."""
    _HANDLERS[handler.content_type] = handler


def get_handler(content_type: str) -> ContentTypeHandler | None:
    """Look up a handler by content type name."""
    return _HANDLERS.get(content_type)


def get_all_handlers() -> dict[str, ContentTypeHandler]:
    """Return a copy of the registered handlers (for inspection/testing)."""
    return dict(_HANDLERS)


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobHandler:
    """Handler for job posting content type."""

    @property
    def content_type(self) -> str:
        return "job"

    @property
    def required_on_create(self) -> tuple[str, ...]:
        return ("title", "description")

    @property
    def field_questions(self) -> dict[str, str]:
        return {
            "title": "What's the job title?",
            "description": "Can you describe the job?",
            "location": "Where is the job located?",
            "remote": "Is the role remote, on-site, or hybrid?",
            "category": "What category is the job?",
        }

    async def apply(
        self,
        intent: dict[str, Any],
        client: WordPressClient,
    ) -> Any:
        action = intent["action"]
        fields = intent["fields"]

        if action == "create":
            return await client.create_post("job", fields)

        # update/delete: find by title
        title = fields.get("title")
        if not title:
            raise WordPressError("job update/delete requires a title to identify the post")
        post_id = await client.find_post_by_title("job", str(title))
        if post_id is None:
            raise WordPressError(f"no job posting titled {title!r} was found to {action}")

        if action == "update":
            return await client.update_post(post_id, fields, content_type="job")
        return await client.delete_post(post_id, content_type="job")


@dataclass(frozen=True)
class AnnouncementHandler:
    """Handler for announcement content type."""

    @property
    def content_type(self) -> str:
        return "announcement"

    @property
    def required_on_create(self) -> tuple[str, ...]:
        return ("title", "body")

    @property
    def field_questions(self) -> dict[str, str]:
        return {
            "title": "What should the announcement be titled?",
            "body": "What should the announcement say?",
        }

    async def apply(
        self,
        intent: dict[str, Any],
        client: WordPressClient,
    ) -> Any:
        action = intent["action"]
        fields = intent["fields"]

        if action == "create":
            return await client.create_post("announcement", fields)

        # update/delete: find by title
        title = fields.get("title")
        if not title:
            raise WordPressError("announcement update/delete requires a title")
        post_id = await client.find_post_by_title("announcement", str(title))
        if post_id is None:
            raise WordPressError(f"no announcement titled {title!r} was found to {action}")

        if action == "update":
            return await client.update_post(post_id, fields, content_type="announcement")
        return await client.delete_post(post_id, content_type="announcement")


@dataclass(frozen=True)
class BusinessInfoHandler:
    """Handler for business_info content type."""

    @property
    def content_type(self) -> str:
        return "business_info"

    @property
    def required_on_create(self) -> tuple[str, ...]:
        return ()  # business_info is always partial

    @property
    def field_questions(self) -> dict[str, str]:
        return {
            "phone": "What phone number should I update to?",
            "hours": "What are the new opening hours?",
            "address": "What's the address?",
            "prices": "What are the new prices?",
        }

    async def apply(
        self,
        intent: dict[str, Any],
        client: WordPressClient,
    ) -> Any:
        return await client.update_site_option(intent["fields"])


@dataclass(frozen=True)
class ImageHandler:
    """Handler for image content type (v1.5)."""

    @property
    def content_type(self) -> str:
        return "image"

    @property
    def required_on_create(self) -> tuple[str, ...]:
        return ("slot",)

    @property
    def field_questions(self) -> dict[str, str]:
        return {
            "slot": "Where should the image go — homepage banner, logo, or gallery?",
            "media_url": "Please send the image you'd like to use.",
        }

    async def apply(
        self,
        intent: dict[str, Any],
        client: WordPressClient,
    ) -> Any:
        import base64

        fields = intent["fields"]
        slot = fields.get("slot")
        if slot is None:
            raise WordPressError("image intent requires a slot")

        media: dict[str, Any] = {}
        if fields.get("media_base64"):
            try:
                media["content"] = base64.b64decode(fields["media_base64"])
            except (ValueError, TypeError) as exc:
                raise WordPressError("media_base64 is not valid base64") from exc
            media["filename"] = f"wpbot-{slot}.img"
            media["mime_type"] = "application/octet-stream"
        elif fields.get("media_url"):
            raise WordPressError(
                "image intents with media_url are not supported yet — send "
                "media_base64 instead (v1.5)"
            )
        else:
            raise WordPressError("image intent requires media_base64 or media_url")

        return await client.upload_and_replace_image(slot, media)


# ---------------------------------------------------------------------------
# Register built-in handlers at module load time
# ---------------------------------------------------------------------------

register_handler(JobHandler())
register_handler(AnnouncementHandler())
register_handler(BusinessInfoHandler())
register_handler(ImageHandler())
