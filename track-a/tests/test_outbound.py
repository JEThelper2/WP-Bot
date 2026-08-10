"""A5 outbound flow: confirmation -> YES/NO -> Track B result -> final reply.

The router drives the exchange: a confirmation-ready intent is composed
and sent, a YES submits it to Track B and replies per the result object,
a NO cancels with no write call ever made. The sender is a fake capturing
exact outbound text; Track B is scripted per status, plus one test runs
the whole flow against the real Track B stub.
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
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    async def submit_intent(self, intent: dict) -> dict:
        self.calls.append(intent)
        return self.results.pop(0)


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
    # Track B was called with the exact intent, once.
    assert trackb.calls == [intent]
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
        result("failed", error_message="no such content on the site")
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
    trackb.results.append(result("success", live_url="https://example.com/x"))
    outcome = handle(router, "yes")
    assert outcome.reason == "publish_success"
    assert sender.last_text == compose_completion("https://example.com/x")
    assert router.sessions.get(OWNER) is None


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
    # Only the one (failed) submit happened; the NO made no write call.
    assert len(trackb.calls) == 1
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


def test_trackb_contract_violation_sends_generic_error():
    intent = make_intent()
    sender = FakeSender()

    class BrokenTrackB:
        async def submit_intent(self, intent):
            raise TrackBError("result fails the contract")

    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        BrokenTrackB(),
        intent,
    )
    outcome = handle(router, "yes")
    assert outcome.reason == "submit_error"
    assert sender.last_text == GENERIC_ERROR_REPLY_TEXT
    assert "Done!" not in sender.last_text


# ------------------------------------------------------------------- NO


def test_no_cancels_without_any_write_call():
    intent = make_intent()
    sender = FakeSender()
    trackb = FakeTrackB()  # would fail loudly if called
    router = start_confirmation(
        ScriptedParser(IntentParseResult(status="intent", intent=intent, confidence=0.95)),
        sender,
        trackb,
        intent,
    )

    outcome = handle(router, "no")
    assert outcome.reason == "cancelled"
    assert sender.last_text == CANCEL_REPLY_TEXT
    assert trackb.calls == []  # no write call was ever made
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


# ------------------------------------------------- end-to-end vs real stub


def test_end_to_end_against_real_track_b_stub():
    """Webhook-in -> confirmation -> YES -> real stub -> completion out."""
    intent = make_intent()
    sender = FakeSender()
    from track_b.stub import create_stub_app

    transport = httpx.ASGITransport(app=create_stub_app())
    http = httpx.AsyncClient(transport=transport, base_url="http://track-b")
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
    # The stub echoes a live_url for the owner.
    assert sender.last_text == (
        f"Done! Here's the live change: https://example.com/owners/{OWNER}. "
        "You can undo this within 24h by replying UNDO."
    )
    assert router.sessions.get(OWNER) is None
