"""Undo engine: reverse-apply of logged state, never a re-guess.

Required cases: undo after create (deletes), after update (restores exact
fields), after delete (restores content), outside the window (rejected
with a clear reason), and with no prior change (nothing to undo). Plus:
every write is logged (PRD §11), an unlogged write is a failure, and the
undo itself is logged and undoable.
"""

import asyncio

import httpx
import pytest
from wp_fake import SITE, FakeWordPress

from shared_contract import CONTRACT_VERSION
from track_b.allowlist import PILOT_SITE_CONFIG, apply_intent
from track_b.changelog import InMemoryChangeLog
from track_b.undo import UNDO_WINDOW_SECONDS, undo
from track_b.wordpress import WordPressClient

OWNER = "15551234567"


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_intent(action, content_type, fields):
    return {
        "contract_version": CONTRACT_VERSION,
        "owner_id": OWNER,
        "action": action,
        "content_type": content_type,
        "fields": fields,
        "confidence": 0.95,
    }


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def stack(clock: Clock):
    fake = FakeWordPress()
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    client = WordPressClient(SITE, "editor", "app-password", client=http)
    changelog = InMemoryChangeLog(time_fn=clock.time)
    return fake, client, changelog


def apply(stack, intent):
    _, client, changelog = stack
    return run(apply_intent(intent, PILOT_SITE_CONFIG, client, changelog))


def undo_now(stack, clock):
    _, client, changelog = stack
    return run(undo(OWNER, client, changelog, now_fn=clock.time))


# ------------------------------------------------------- the required cases


def test_undo_after_create_deletes_the_post(stack, clock):
    created = apply(
        stack, make_intent("create", "job", {"title": "Barista", "description": "$18/hr"})
    )
    post_id = created["after"]["post_id"]
    assert stack[0].posts[post_id]["status"] == "publish"

    result = undo_now(stack, clock)

    assert result.status == "undone"
    assert result.original_change_id == created["change_id"]
    # The created post is gone (trashed) on the site.
    assert stack[0].posts[post_id]["status"] == "trash"
    # The undo itself is logged, linked to the original.
    row = run(stack[2].most_recent(OWNER))
    assert row.action == "undo"
    assert row.undo_of == created["change_id"]
    assert row.before == created["after"]  # state before the undo


def test_undo_after_update_restores_exact_prior_fields(stack, clock):
    apply(stack, make_intent("create", "job", {"title": "Barista", "description": "$18/hr"}))
    updated = apply(
        stack,
        make_intent("update", "job", {"title": "Barista", "description": "$20/hr"}),
    )
    assert updated["after"]["content"] == "$20/hr"

    result = undo_now(stack, clock)

    assert result.status == "undone"
    post_id = updated["after"]["post_id"]
    post = stack[0].posts[post_id]
    assert post["title"]["raw"] == "Barista"
    assert post["content"]["raw"] == "$18/hr"  # exactly the prior value
    assert post["status"] == "publish"


def test_undo_after_delete_restores_the_content(stack, clock):
    apply(stack, make_intent("create", "job", {"title": "Barista", "description": "$18/hr"}))
    deleted = apply(stack, make_intent("delete", "job", {"title": "Barista"}))
    assert deleted["status"] == "success"

    result = undo_now(stack, clock)

    assert result.status == "undone"
    assert result.live_url  # the re-created post is live again
    # A new post exists with the exact stored pre-delete state.
    restored = run(stack[1].find_post_by_title("job", "Barista"))
    assert restored is not None
    post = stack[0].posts[restored]
    assert post["content"]["raw"] == "$18/hr"
    row = run(stack[2].most_recent(OWNER))
    assert row.action == "undo"
    assert row.undo_of == deleted["change_id"]


def test_undo_outside_window_is_rejected_with_reason(stack, clock):
    apply(stack, make_intent("create", "job", {"title": "Barista", "description": "$18/hr"}))
    clock.advance(UNDO_WINDOW_SECONDS + 1)

    result = undo_now(stack, clock)

    assert result.status == "window_passed"
    assert "24h" in result.message
    assert "no longer available" in result.message
    # The post was NOT touched.
    assert len(stack[0].posts) == 1
    # And nothing was logged for the rejected attempt.
    assert run(stack[2].most_recent(OWNER)).action == "create"


def test_undo_with_no_prior_change_is_clear(stack, clock):
    result = undo_now(stack, clock)
    assert result.status == "nothing_to_undo"
    assert "no changes found" in result.message


# ------------------------------------------------------- robustness


def test_undo_at_exact_window_boundary_is_allowed(stack, clock):
    apply(stack, make_intent("create", "job", {"title": "Barista", "description": "$18/hr"}))
    clock.advance(UNDO_WINDOW_SECONDS)
    result = undo_now(stack, clock)
    assert result.status == "undone"


def test_undo_is_itself_undoable(stack, clock):
    apply(stack, make_intent("create", "job", {"title": "Barista", "description": "$18/hr"}))
    apply(stack, make_intent("update", "job", {"title": "Barista", "description": "$20/hr"}))

    first = undo_now(stack, clock)  # restores $18/hr
    assert first.status == "undone"
    assert stack[0].posts[1]["content"]["raw"] == "$18/hr"

    second = undo_now(stack, clock)  # undoes the undo -> $20/hr again
    assert second.status == "undone"
    post = stack[0].posts[1]
    assert post["content"]["raw"] == "$20/hr"
    row = run(stack[2].most_recent(OWNER))
    assert row.action == "undo"
    assert row.undo_of == first.change_id  # trail links undo -> undo


def test_undo_failed_reverse_apply_is_not_logged(stack, clock):
    apply(stack, make_intent("create", "job", {"title": "Barista", "description": "$18/hr"}))
    stack[0].inject_status = 500  # make the next WordPress call fail

    result = undo_now(stack, clock)

    assert result.status == "failed"
    assert "undo failed" in result.message
    # No undo row was recorded; the original row is still the most recent.
    row = run(stack[2].most_recent(OWNER))
    assert row.action == "create"


# ------------------------------------------------------- PRD §11 logging


def test_every_successful_write_is_logged(stack, clock):
    result = apply(
        stack, make_intent("create", "job", {"title": "Barista", "description": "$18/hr"})
    )

    row = run(stack[2].most_recent(OWNER))
    assert row.change_id == result["change_id"]  # same id as the result
    assert row.owner_id == OWNER
    assert row.content_type == "job"
    assert row.action == "create"
    assert row.before is None
    assert row.after == result["after"]
    assert row.live_url == result["live_url"]


def test_a_write_that_cannot_be_logged_is_a_failure(stack, clock):
    _, client, _ = stack

    class BrokenLog:
        async def record_change(self, row):
            raise RuntimeError("database unavailable")

        async def most_recent(self, owner_id):
            return None

        async def get(self, change_id):
            return None

    result = run(
        apply_intent(
            make_intent("create", "job", {"title": "Barista", "description": "$18/hr"}),
            PILOT_SITE_CONFIG,
            client,
            BrokenLog(),
        )
    )

    assert result["status"] == "failed"
    assert "could not be logged" in result["error_message"]
