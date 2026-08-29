"""Phase 8: Owner-facing features (§10).

Covers:
1. §10 Recap command: match {what have i changed, recent changes, recap, history}
   → query action_log for that tenant, last 5 entries, reply as numbered list
   with relative timestamps.
2. §10 Draft/preview: for business_info_update and page_content_update,
   the AWAITING_CONFIRMATION message includes a before/after diff.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytest

from track_a.composer import (
    compose_confirmation,
    compose_confirmation_with_diff,
)
from track_a.routing import IntentRouter, RouteOutcome, _is_recap, _relative_time, _change_summary
from track_a.session import SessionState, SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

OWNER = "15551234567"


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: str, text: str) -> None:
        self.sent.append((to, text))

    @property
    def last_text(self) -> str:
        return self.sent[-1][1] if self.sent else ""


class FakeTrackB:
    def __init__(self, changes: list[dict] | None = None) -> None:
        self.changes = changes or []
        self.calls: list[tuple[str, Any]] = []

    async def list_changes(self, owner_id: str, *, limit: int = 5) -> list[dict]:
        self.calls.append(("list_changes", owner_id))
        return self.changes[:limit]

    async def submit_intent(self, intent: dict, *, decision: str | None = None) -> dict:
        self.calls.append(("submit_intent", intent))
        from shared_contract import CONTRACT_VERSION
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "needs_confirmation",
            "change_id": "ch-test",
            "before": intent.get("before_state"),
            "after": intent.get("fields"),
            "live_url": None,
            "error_message": None,
        }

    async def undo(self, owner_id: str, *, site_id: str | None = None) -> dict:
        self.calls.append(("undo", owner_id))
        from shared_contract import CONTRACT_VERSION
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "change_id": "ch-undo",
            "before": None,
            "after": None,
            "live_url": None,
            "error_message": None,
        }

    async def set_active_site(self, site_id: str, owner_id: str) -> bool:
        return True

    async def list_sites(self, owner_id: str) -> list[dict]:
        return []


class FakeParser:
    """Returns a fixed parse result."""
    def __init__(self, result: Any = None) -> None:
        self._result = result

    async def parse(self, text: str, owner_id: str, *, context: str | None = None) -> Any:
        return self._result


def make_router(trackb: FakeTrackB | None = None) -> IntentRouter:
    from track_a.intent import IntentParseResult
    parser = FakeParser(IntentParseResult(status="unsupported", intent=None, confidence=0.0))
    return IntentRouter(
        parser=parser,
        sender=FakeSender(),
        trackb=trackb or FakeTrackB(),
    )


# ---------------------------------------------------------------------------
# §10: _is_recap matching
# ---------------------------------------------------------------------------

class TestRecapMatching:
    def test_exact_matches(self):
        assert _is_recap("recap")
        assert _is_recap("history")
        assert _is_recap("recent changes")
        assert _is_recap("what have i changed")
        assert _is_recap("what have i done")
        assert _is_recap("show my changes")

    def test_case_insensitive(self):
        assert _is_recap("Recap")
        assert _is_recap("RECAP")
        assert _is_recap("History")

    def test_with_punctuation(self):
        assert _is_recap("recap!")
        assert _is_recap("history?")
        assert _is_recap("what have i changed?")

    def test_no_match(self):
        assert not _is_recap("hello")
        assert not _is_recap("add a job")
        assert not _is_recap("undo")
        assert not _is_recap("")


# ---------------------------------------------------------------------------
# §10: _relative_time
# ---------------------------------------------------------------------------

class TestRelativeTime:
    def test_none_returns_recently(self):
        assert _relative_time(None) == "recently"

    def test_empty_returns_recently(self):
        assert _relative_time("") == "recently"

    def test_just_now(self):
        now = datetime.now(UTC).isoformat()
        assert _relative_time(now) == "just now"

    def test_minutes_ago(self):
        now = datetime.now(UTC)
        ts = (now.timestamp() - 180)  # 3 minutes ago
        iso = datetime.fromtimestamp(ts, tz=UTC).isoformat()
        result = _relative_time(iso)
        assert "minute" in result
        assert "ago" in result

    def test_hours_ago(self):
        now = datetime.now(UTC)
        ts = now.timestamp() - 7200  # 2 hours ago
        iso = datetime.fromtimestamp(ts, tz=UTC).isoformat()
        result = _relative_time(iso)
        assert "hour" in result
        assert "ago" in result

    def test_days_ago(self):
        now = datetime.now(UTC)
        ts = now.timestamp() - 172800  # 2 days ago
        iso = datetime.fromtimestamp(ts, tz=UTC).isoformat()
        result = _relative_time(iso)
        assert "day" in result
        assert "ago" in result

    def test_invalid_iso_returns_recently(self):
        assert _relative_time("not-a-date") == "recently"


# ---------------------------------------------------------------------------
# §10: _change_summary
# ---------------------------------------------------------------------------

class TestChangeSummary:
    def test_job_create(self):
        change = {"action": "create", "content_type": "job", "after": {"title": "Cashier"}}
        assert "Cashier" in _change_summary(change)

    def test_job_update(self):
        change = {"action": "update", "content_type": "job", "after": {"title": "Chef"}}
        summary = _change_summary(change)
        assert "updated" in summary.lower()
        assert "Chef" in summary

    def test_announcement_delete(self):
        change = {"action": "delete", "content_type": "announcement", "after": {"title": "Sale"}}
        summary = _change_summary(change)
        assert "removed" in summary.lower()
        assert "Sale" in summary

    def test_business_info(self):
        change = {"action": "update", "content_type": "business_info", "after": {"phone": "0801234"}}
        summary = _change_summary(change)
        assert "phone" in summary

    def test_undo_action(self):
        change = {"action": "undo", "content_type": "job", "after": {}}
        summary = _change_summary(change)
        assert "changed" in summary.lower()


# ---------------------------------------------------------------------------
# §10: Recap command via router
# ---------------------------------------------------------------------------

class TestRecapCommand:
    @pytest.mark.anyio
    async def test_recap_with_changes(self):
        changes = [
            {
                "change_id": "ch-1",
                "action": "create",
                "content_type": "job",
                "before": None,
                "after": {"title": "Cashier"},
                "created_at": datetime.now(UTC).isoformat(),
            },
            {
                "change_id": "ch-2",
                "action": "update",
                "content_type": "business_info",
                "before": {"phone": "0801"},
                "after": {"phone": "0802"},
                "created_at": datetime.now(UTC).isoformat(),
            },
        ]
        trackb = FakeTrackB(changes=changes)
        router = make_router(trackb)
        sender = router.sender

        outcome = await router.handle_message(OWNER, "recap")
        assert outcome.reason == "recap_shown"
        assert "recent changes" in sender.last_text.lower() or "recent change" in sender.last_text.lower()
        assert "1." in sender.last_text  # numbered list
        assert "2." in sender.last_text

    @pytest.mark.anyio
    async def test_recap_empty(self):
        trackb = FakeTrackB(changes=[])
        router = make_router(trackb)
        sender = router.sender

        outcome = await router.handle_message(OWNER, "history")
        assert outcome.reason == "recap_empty"
        assert "no changes" in sender.last_text.lower() or "no change" in sender.last_text.lower()

    @pytest.mark.anyio
    async def test_recap_calls_trackb(self):
        trackb = FakeTrackB(changes=[])
        router = make_router(trackb)

        await router.handle_message(OWNER, "recent changes")
        list_calls = [c for c in trackb.calls if c[0] == "list_changes"]
        assert len(list_calls) == 1
        assert list_calls[0][1] == OWNER

    @pytest.mark.anyio
    async def test_recap_with_undo_entries(self):
        changes = [
            {
                "change_id": "ch-1",
                "action": "undo",
                "content_type": "job",
                "before": None,
                "after": {},
                "created_at": datetime.now(UTC).isoformat(),
            },
        ]
        trackb = FakeTrackB(changes=changes)
        router = make_router(trackb)
        sender = router.sender

        outcome = await router.handle_message(OWNER, "recap")
        assert "reverted" in sender.last_text.lower() or "undo" in sender.last_text.lower()

    @pytest.mark.anyio
    async def test_recap_error_fallback(self):
        trackb = FakeTrackB()
        original = trackb.list_changes

        async def failing_list(*args, **kwargs):
            raise ConnectionError("network error")

        trackb.list_changes = failing_list
        router = make_router(trackb)
        sender = router.sender

        outcome = await router.handle_message(OWNER, "recap")
        # Should fall back to empty message, not crash
        assert outcome.reason in ("recap_error", "recap_empty")
        trackb.list_changes = original

    @pytest.mark.anyio
    async def test_recap_blocked_during_confirmation(self):
        """Recap should not work when a confirmation is pending."""
        router = make_router()
        # Set up AWAITING_CONFIRMATION state
        router.sessions.set(OWNER, SessionState(
            state="AWAITING_CONFIRMATION",
            pending_intent={"action": "delete", "content_type": "job"},
        ))

        # "recap" should be treated as a confirmation reply, not trigger recap
        outcome = await router.handle_message(OWNER, "recap")
        # Since "recap" is not yes/no, it should trigger re-ask
        assert outcome.reason in ("confirmation_reask", "confirmation_cancelled")


# ---------------------------------------------------------------------------
# §10: Draft/preview for high-impact edits
# ---------------------------------------------------------------------------

class TestDraftPreview:
    def test_business_info_diff_single_field(self):
        intent = {
            "action": "update",
            "content_type": "business_info",
            "fields": {"phone": "08029876543"},
        }
        before = {"phone": "08012345678"}
        after = {"phone": "08029876543"}

        msg = compose_confirmation_with_diff(intent, before, after)
        assert "08012345678" in msg
        assert "08029876543" in msg
        assert "yes" in msg.lower() or "confirm" in msg.lower()

    def test_business_info_diff_multiple_fields(self):
        intent = {
            "action": "update",
            "content_type": "business_info",
            "fields": {"phone": "0802", "address": "123 Main St"},
        }
        before = {"phone": "0801", "address": "456 Old St"}
        after = {"phone": "0802", "address": "123 Main St"}

        msg = compose_confirmation_with_diff(intent, before, after)
        assert "0801" in msg
        assert "0802" in msg
        assert "456 Old St" in msg
        assert "123 Main St" in msg

    def test_business_info_no_change(self):
        """When values haven't actually changed, fall back to standard confirmation."""
        intent = {
            "action": "update",
            "content_type": "business_info",
            "fields": {"phone": "0801"},
        }
        before = {"phone": "0801"}
        after = {"phone": "0801"}

        msg = compose_confirmation_with_diff(intent, before, after)
        # Should fall back to standard confirmation (no old value shown)
        assert "0801" in msg

    def test_non_business_info_falls_back(self):
        intent = {
            "action": "delete",
            "content_type": "job",
            "fields": {"title": "Cashier"},
        }
        msg = compose_confirmation_with_diff(intent, {"title": "Cashier"}, {"title": "Cashier"})
        # Should fall back to standard confirmation
        assert "Cashier" in msg

    def test_standard_confirmation_still_works(self):
        """compose_confirmation is unchanged."""
        intent = {
            "action": "create",
            "content_type": "job",
            "fields": {"title": "Chef", "description": "Full time"},
        }
        msg = compose_confirmation(intent)
        assert "Chef" in msg
        assert "Full time" in msg
