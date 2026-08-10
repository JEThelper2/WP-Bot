"""A5 outbound flow: confirmation -> YES/NO -> Track B result -> final reply.

The router drives the exchange (Integration Phase flow): a
confirmation-ready intent is STAGED at Track B first, the confirmation is
composed and sent, a YES resolves it (`decision=yes`) and replies per the
result object, a NO relays the discard (`decision=no`) — a WordPress write
is never made on NO. The sender is a fake capturing exact outbound text;
Track B is scripted per status. The full webhook-driven end-to-end flows
live in tests/test_integration_phase.py.
"""

import asyncio
from typing import Any

import httpx
import pytest
from shared_contract import CONTRACT_VERSION

from track_a.composer import (
    CANCEL_REPLY_TEXT,
    GENERIC_ERROR_REPLY_TEXT,
    compose_completion,
    compose_confirmation,
    compose_error,
)
from track_a.intent import IntentParseResult
from track_a.routing import IntentRouter
from track_a.session import SessionStore
from track_a.trackb import TrackBClient, TrackBError

OWNER = "15551234567"


def make_intent(action="create", content_type="job", fields=None, confidence=0.95):
    return {
        "contract_version": CONTRACT_VERSION,
        "owner_id": OWNER,
        "action": action,
        "content_type": content_type,
        "fields": fields or {"title": "Cashier", "description": "evenings"},
        "confidence": confidence,
    }


def result(status: str, **overrides) -> dict:
    r = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "change_id": "ch-1",
        "before": None,
        "after": None,
        "live_url": None,
        "error_message": None,
    }
    r.update(overrides)
    return r


class ScriptedParser:
    def __init__(self, *results):
        self.results = list(results)

    async def parse(self, message_text, owner_id, *, context=None):
        return self.results.pop(0)


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: str, text: str) -> None:
        self.sent.append((to, text))

    @property
    def last_text(self) -> str:
        return self.sent[-1][1]


class FakeTrackB:
    """Realistic Track B double: stages, then resolves per scripted results."""

    def __init__(self, *resolve_results):
        self.resolve_results = list(resolve_results)
        self.calls: list[tuple[dict, str | None]] = []

    async def submit_intent(self, intent: dict, *, decision: str | None = None) -> dict:
        self.calls.append((intent, decision))
        if decision is None:
            return result("needs_confirmation", change_id="pc-1")
        if decision == "no":
            return result("success")  # discarded; nothing written
        return self.resolve_results.pop(0)

    async def undo(self, owner_id: str) -> dict:
        raise AssertionError("undo is exercised by the integration suite")


def build_router(parser, trackb, sender):
    return IntentRouter(
        parser=parser, sessions=SessionStore(), sender=sender, trackb=trackb
    )


def handle(router, text):
    return asyncio.run(router.handle_message(OWNER, text))


def start_confirmation(parser, sender, trackb, intent):
    """Drive a message that becomes confirmation-ready; return the router."""
    router = build_router(parser, trackb, sender)
    handle(router, "post the job")
    assert sender.last_text == compose_confirmation(intent)
    return router


# ------------------------------------------------------------------ YES


def test_yes_success_sends_completion_with_live_url():
    intent = make_intent()
    sender = FakeSender()
    trackb = FakeTrackB(
        result("success", live_url="https://example.com/owners/15551234567")
    )
    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        trackb,
        intent,
    )

    outcome = handle(router, "yes")
    assert outcome.reason == "publish_success"
    # Staged first, then resolved with decision=yes — same intent both times.
    assert trackb.calls == [(intent, None), (intent, "yes")]
    # Exact completion message out.
    assert sender.last_text == (
        "Done! Here's the live change: https://example.com/owners/15551234567. "
        "You can undo this within 24h by replying UNDO."
    )
    assert router.sessions.get(OWNER) is None


def test_yes_failed_sends_error_with_message_and_keeps_intent_for_retry():
    intent = make_intent()
    sender = FakeSender()
    trackb = FakeTrackB(
        result("failed", error_message="no such content on the site"),
        result("success", live_url="https://example.com/x"),
    )
    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        trackb,
        intent,
    )

    outcome = handle(router, "yes")
    assert outcome.reason == "publish_failed"
    assert sender.last_text == compose_error("no such content on the site")
    assert "no such content on the site" in sender.last_text
    assert "Nothing was published" in sender.last_text
    # Pending intent kept: "Want to try again?" -> a later YES retries.
    assert router.sessions.get(OWNER).pending_intent == intent
    outcome = handle(router, "yes")
    assert outcome.reason == "publish_success"
    assert sender.last_text == compose_completion("https://example.com/x")
    assert router.sessions.get(OWNER) is None
    assert trackb.calls == [(intent, None), (intent, "yes"), (intent, "yes")]


def test_cancel_after_failed_clears_pending_without_retry():
    intent = make_intent()
    sender = FakeSender()
    trackb = FakeTrackB(result("failed", error_message="no such content on the site"))
    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        trackb,
        intent,
    )

    handle(router, "yes")  # failed -> error out, intent kept
    assert router.sessions.get(OWNER).pending_intent == intent
    outcome = handle(router, "no")
    assert outcome.reason == "cancelled"
    assert sender.last_text == CANCEL_REPLY_TEXT
    # Stage + failed YES + discard-NO relayed; the NO never publishes.
    assert trackb.calls == [(intent, None), (intent, "yes"), (intent, "no")]
    assert router.sessions.get(OWNER) is None


def test_yes_failed_without_message_uses_generic_error():
    intent = make_intent()
    sender = FakeSender()
    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        FakeTrackB(result("failed")),
        intent,
    )
    outcome = handle(router, "yes")
    assert outcome.reason == "publish_failed"
    assert sender.last_text == GENERIC_ERROR_REPLY_TEXT


def test_yes_needs_confirmation_resends_prompt_defensively():
    intent = make_intent()
    sender = FakeSender()
    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        FakeTrackB(result("needs_confirmation")),
        intent,
    )
    outcome = handle(router, "yes")
    assert outcome.reason == "needs_confirmation"
    # The confirmation prompt is re-sent, still awaiting a decision.
    assert sender.last_text == compose_confirmation(intent)
    assert router.sessions.get(OWNER).pending_intent == intent


def test_yes_unexpected_status_is_error_never_fake_success():
    intent = make_intent()
    sender = FakeSender()
    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        FakeTrackB(result("bogus")),
        intent,
    )
    outcome = handle(router, "yes")
    assert outcome.reason == "unexpected_status"
    assert "Unexpected response" in sender.last_text
    assert "Done!" not in sender.last_text


def test_stage_contract_violation_sends_generic_error():
    """A Track B that breaks the contract at STAGE time: no confirmation
    is ever sent — the owner gets a generic error instead."""
    intent = make_intent()
    sender = FakeSender()

    class BrokenTrackB:
        async def submit_intent(self, intent, *, decision=None):
            raise TrackBError("result fails the contract")

    router = build_router(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        BrokenTrackB(),
        sender,
    )
    outcome = handle(router, "post the job")
    assert outcome.reason == "stage_failed"
    assert sender.last_text == GENERIC_ERROR_REPLY_TEXT
    assert "Done!" not in sender.last_text
    # Nothing held for confirmation.
    assert router.sessions.get(OWNER) is None


def test_trackb_contract_violation_on_yes_sends_generic_error():
    """A Track B that breaks the contract at RESOLVE time: the pending
    intent is kept so the owner can retry after a generic error."""
    intent = make_intent()
    sender = FakeSender()

    class BrokenOnResolve:
        async def submit_intent(self, intent, *, decision=None):
            if decision is not None:
                raise TrackBError("result fails the contract")
            return result("needs_confirmation", change_id="pc-1")

    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        BrokenOnResolve(),
        intent,
    )
    outcome = handle(router, "yes")
    assert outcome.reason == "submit_error"
    assert sender.last_text == GENERIC_ERROR_REPLY_TEXT
    assert "Done!" not in sender.last_text
    assert router.sessions.get(OWNER).pending_intent == intent


# ------------------------------------------------------------------- NO


def test_no_cancels_and_relays_discard_never_publishes():
    intent = make_intent()
    sender = FakeSender()
    trackb = FakeTrackB()
    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        trackb,
        intent,
    )

    outcome = handle(router, "no")
    assert outcome.reason == "cancelled"
    assert sender.last_text == CANCEL_REPLY_TEXT
    # Staged, then the NO was relayed as a discard — never a publish.
    assert trackb.calls == [(intent, None), (intent, "no")]
    assert router.sessions.get(OWNER) is None  # pending intent discarded


def test_ambiguous_reply_resends_confirmation():
    intent = make_intent()
    sender = FakeSender()
    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        FakeTrackB(),
        intent,
    )
    outcome = handle(router, "maybe?")
    assert outcome.reason == "confirmation_repeat"
    assert sender.last_text == compose_confirmation(intent)
    # Still awaiting a decision; Track B untouched.
    assert router.sessions.get(OWNER).pending_intent == intent


# ------------------------------------------------- end-to-end vs real API


def test_end_to_end_confirmation_flow_against_real_track_b_api(tmp_path):
    """Confirmation -> YES -> real Track B API (fake WordPress) -> completion.

    The webhook-driven version of this flow (and the undo / clarification /
    escalation flows) lives in tests/test_integration_phase.py; this runs
    the same round-trip at the router level.
    """
    intent = make_intent()
    sender = FakeSender()

    import httpx as _httpx
    from track_b.allowlist import PILOT_SITE_CONFIG
    from track_b.changelog import InMemoryChangeLog
    from track_b.config import Settings as BSettings
    from track_b.main import TrackBServices, create_app as create_track_b_app
    from track_b.onboarding import OnboardedSiteStore
    from track_b.pending import InMemoryPendingStore
    from track_b.wordpress import WordPressClient
    from wp_fake import SITE, FakeWordPress

    fake = FakeWordPress(expected_auth=("editor", "app-pass"))
    sites = OnboardedSiteStore(tmp_path / "sites.db")
    sites.add_site(
        owner_id=OWNER,
        site_url=SITE,
        username="editor",
        app_password="app-pass",
        allowlist=PILOT_SITE_CONFIG,
    )

    def make_client(site):
        return WordPressClient(
            site.site_url,
            "editor",
            "app-pass",
            client=_httpx.AsyncClient(
                transport=_httpx.MockTransport(fake.handler)
            ),
        )

    services = TrackBServices(
        sites=sites,
        pending=InMemoryPendingStore(),
        changelog=InMemoryChangeLog(),
        make_client=make_client,
    )
    transport = _httpx.ASGITransport(
        app=create_track_b_app(settings=BSettings(db_path=tmp_path / "tb.db"), services=services)
    )
    http = _httpx.AsyncClient(transport=transport, base_url="http://track-b")
    trackb = TrackBClient(base_url="http://track-b", client=http)

    router = build_router(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        trackb,
        sender,
    )
    handle(router, "post the job")
    assert sender.last_text == compose_confirmation(intent)

    outcome = handle(router, "yes")
    assert outcome.reason == "publish_success"
    # The real API returns the created post's live_url.
    assert sender.last_text == compose_completion(f"{SITE}/?p=1")
    assert fake.posts  # the write actually landed on (fake) WordPress
    assert router.sessions.get(OWNER) is None
