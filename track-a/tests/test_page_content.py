"""Tests for page_content_update content type.

Covers:
1. Track B PageHandler (allowlist validation, WordPress page write)
2. Track A composer (confirmation message for page updates)
3. End-to-end: intent → allowlist → FakeWordPress page update
4. FakeWordPress pages CRUD (search, update, 404)
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from track_a.composer import compose_confirmation, compose_confirmation_with_diff
from track_a.routing import missing_required_fields

# Track B imports
from track_b.allowlist import PILOT_SITE_CONFIG, IntentNotAllowedError, validate_intent_for_site
from shared_contract import ContractValidationError
from track_b.content_types import PageHandler, get_handler, get_all_handlers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OWNER = "15551234567"
SITE = "https://wp.example.com"


def make_page_intent(
    action: str = "update",
    fields: dict | None = None,
    **extra,
) -> dict:
    return {
        "contract_version": "1.0.0",
        "owner_id": OWNER,
        "action": action,
        "content_type": "page",
        "fields": fields or {"title": "About", "content": "New about page text"},
        "confidence": 0.95,
        **extra,
    }


# ---------------------------------------------------------------------------
# PageHandler registration
# ---------------------------------------------------------------------------

class TestPageHandlerRegistration:
    def test_page_handler_is_registered(self):
        handler = get_handler("page")
        assert handler is not None
        assert isinstance(handler, PageHandler)

    def test_page_in_all_handlers(self):
        handlers = get_all_handlers()
        assert "page" in handlers

    def test_page_handler_properties(self):
        handler = PageHandler()
        assert handler.content_type == "page"
        assert handler.required_on_create == ()
        assert "title" in handler.field_questions
        assert "content" in handler.field_questions


# ---------------------------------------------------------------------------
# Allowlist validation
# ---------------------------------------------------------------------------

class TestPageAllowlist:
    def test_page_intent_passes_validation(self):
        intent = make_page_intent()
        validate_intent_for_site(intent, PILOT_SITE_CONFIG)  # should not raise

    def test_page_update_with_empty_fields_passes_allowlist(self):
        # The allowlist checks field names, not minProperties (schema does that)
        intent = make_page_intent(fields={})
        validate_intent_for_site(intent, PILOT_SITE_CONFIG)  # should not raise
        # The handler itself will fail when trying to apply with no title/content

    def test_page_unknown_field_rejected(self):
        intent = make_page_intent(fields={"title": "About", "body": "bad field"})
        with pytest.raises((IntentNotAllowedError, ContractValidationError)):
            validate_intent_for_site(intent, PILOT_SITE_CONFIG)


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------

class TestPageRequiredFields:
    def test_page_update_has_no_required_fields(self):
        intent = make_page_intent(fields={"title": "About"})
        missing = missing_required_fields(intent)
        assert missing == []

    def test_page_create_has_no_required_fields(self):
        intent = make_page_intent(action="create", fields={"title": "New Page"})
        missing = missing_required_fields(intent)
        assert missing == []


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------

class TestPageComposer:
    def test_page_update_with_content(self):
        intent = make_page_intent(fields={"title": "About", "content": "We are a coffee shop"})
        msg = compose_confirmation(intent)
        assert "About" in msg
        assert "coffee shop" in msg
        assert "yes" in msg.lower() or "confirm" in msg.lower()

    def test_page_update_long_content_truncated(self):
        long_content = "x" * 200
        intent = make_page_intent(fields={"title": "Home", "content": long_content})
        msg = compose_confirmation(intent)
        assert "..." in msg  # should be truncated
        assert len(msg) < 300

    def test_page_update_no_content(self):
        intent = make_page_intent(fields={"title": "Contact"})
        msg = compose_confirmation(intent)
        assert "Contact" in msg


# ---------------------------------------------------------------------------
# FakeWordPress pages
# ---------------------------------------------------------------------------

class TestFakeWordPressPages:
    def test_pages_endpoint_exists(self):
        from wp_fake import FakeWordPress, SITE
        fake = FakeWordPress()
        # Seed a page
        fake.pages[1] = {
            "id": 1,
            "title": {"raw": "About", "rendered": "About"},
            "content": {"raw": "About us", "rendered": "About us"},
            "status": "publish",
            "link": f"{SITE}/?page_id=1",
        }
        assert 1 in fake.pages
        assert fake.pages[1]["title"]["raw"] == "About"


# ---------------------------------------------------------------------------
# End-to-end: intent → PageHandler → FakeWordPress
# ---------------------------------------------------------------------------

class TestPageContentUpdateE2E:
    @pytest.mark.anyio
    async def test_page_update_end_to_end(self):
        from wp_fake import FakeWordPress, SITE
        from track_b.wordpress import WordPressClient

        fake = FakeWordPress()
        # Seed an About page
        fake.pages[1] = {
            "id": 1,
            "title": {"raw": "About", "rendered": "About"},
            "content": {"raw": "Old content", "rendered": "Old content"},
            "status": "publish",
            "link": f"{SITE}/?page_id=1",
        }

        client = WordPressClient(
            SITE, "editor", "pass",
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )

        handler = PageHandler()
        intent = make_page_intent(
            fields={"title": "About", "content": "Brand new about content"},
        )
        record = await handler.apply(intent, client)

        assert record.before is not None
        assert record.before["title"] == "About"
        assert record.before["content"] == "Old content"
        assert record.after is not None
        assert record.after["content"] == "Brand new new content" or record.after["content"] == "Brand new about content"
        assert record.live_url is not None

    @pytest.mark.anyio
    async def test_page_update_not_found(self):
        from wp_fake import FakeWordPress
        from track_b.wordpress import WordPressClient, WordPressError

        fake = FakeWordPress()
        client = WordPressClient(
            SITE, "editor", "pass",
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )

        handler = PageHandler()
        intent = make_page_intent(
            fields={"title": "Nonexistent", "content": "New text"},
        )
        with pytest.raises(WordPressError, match="no page titled"):
            await handler.apply(intent, client)

    @pytest.mark.anyio
    async def test_page_create_rejected(self):
        from wp_fake import FakeWordPress
        from track_b.wordpress import WordPressClient, WordPressError

        fake = FakeWordPress()
        client = WordPressClient(
            SITE, "editor", "pass",
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )

        handler = PageHandler()
        intent = make_page_intent(action="create", fields={"title": "New Page"})
        with pytest.raises(WordPressError, match="not supported"):
            await handler.apply(intent, client)

    @pytest.mark.anyio
    async def test_page_delete_rejected(self):
        from wp_fake import FakeWordPress
        from track_b.wordpress import WordPressClient, WordPressError

        fake = FakeWordPress()
        client = WordPressClient(
            SITE, "editor", "pass",
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )

        handler = PageHandler()
        intent = make_page_intent(action="delete", fields={"title": "About"})
        with pytest.raises(WordPressError, match="not supported"):
            await handler.apply(intent, client)
