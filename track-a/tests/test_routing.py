"""A4 routing: state machine transitions per PRODUCTION_SPEC_DETAILED.md §3.

Every parsed intent goes to exactly one of:
- confirm (AWAITING_CONFIRMATION): destructive/high-impact action → YES/NO.
- clarify (AWAITING_CLARIFICATION): low confidence, missing fields, ambiguous.
- unclear → AWAITING_CLARIFICATION: unsupported/unclear intent.

The parser is scripted (like test_intent.py's FakeLLM): the router under
test is real.
"""

import asyncio
from pathlib import Path
from typing import Any

from shared_contract import CONTRACT_VERSION
from track_a import store
from track_a.intent import IntentParseResult
from track_a.i18n import translate
from track_a.routing import (
    CLARIFICATION_MAX_TURNS,
    IntentRouter,
)
from track_a.session import SessionStore

OWNER = "15551234567"


def make_intent(
    action: str,
    content_type: str,
    fields: dict[str, Any],
    confidence: float,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "owner_id": OWNER,
        "action": action,
        "content_type": content_type,
        "fields": fields,
        "confidence": confidence,
    }


class ScriptedParser:
    """Returns scripted parse results per call; records context."""

    def __init__(self, *results: IntentParseResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, str | None]] = []

    async def parse(self, message_text: str, owner_id: str, *, context=None):
        self.calls.append({"text": message_text, "context": context})
        return self.results.pop(0)


class FakeTrackB:
    """Stages every confirmation successfully; resolution is scripted."""

    def __init__(self, *resolve_results: dict) -> None:
        self.resolve_results = list(resolve_results)
        self.calls: list[tuple[dict, str | None]] = []

    async def submit_intent(self, intent: dict, *, decision: str | None = None) -> dict:
        self.calls.append((intent, decision))
        if decision is None:
            return {
                "contract_version": CONTRACT_VERSION,
                "status": "needs_confirmation",
                "change_id": "pc-test",
                "before": None,
                "after": None,
                "live_url": None,
                "error_message": None,
            }
        if decision == "no":
            return {
                "contract_version": CONTRACT_VERSION,
                "status": "success",
                "change_id": "pc-test",
                "before": None,
                "after": None,
                "live_url": None,
                "error_message": None,
            }
        return self.resolve_results.pop(0)

    async def undo(self, owner_id: str) -> dict:
        raise AssertionError("undo should not be exercised by routing tests")


def make_router(
    parser: ScriptedParser, db: Path | None = None, trackb: FakeTrackB | None = None
) -> IntentRouter:
    sessions = SessionStore()
    log_escalation = (
        (lambda owner, msg: store.log_escalation_request(db, owner, msg))
        if db is not None
        else None
    )
    return IntentRouter(
        parser=parser,
        sessions=sessions,
        log_escalation=log_escalation,
        trackb=trackb or FakeTrackB(),
    )


def handle(router: IntentRouter, text: str) -> Any:
    return asyncio.run(router.handle_message(OWNER, text))


def parse_intent(intent: dict[str, Any]) -> IntentParseResult:
    return IntentParseResult(status="intent", intent=intent, confidence=intent["confidence"])


# ---------------------------------------------------------------- confirm


def test_high_confidence_complete_job_goes_to_confirm():
    intent = make_intent(
        "create",
        "job",
        {"title": "Part-time Barista", "description": "$18/hr downtown"},
        0.9,
    )
    trackb = FakeTrackB()
    router = make_router(ScriptedParser(parse_intent(intent)), trackb=trackb)
    outcome = handle(router, "post a job for a barista downtown")
    assert outcome.branch == "confirm"
    assert outcome.intent == intent
    # A5: the confirmation prompt is composed and sent, awaiting YES/NO.
    from track_a.composer import compose_confirmation

    assert outcome.reply_text == compose_confirmation(intent)
    state = router.sessions.get(OWNER)
    assert state is not None and state.state == "AWAITING_CONFIRMATION"
    assert state.pending_intent == intent
    # Integration Phase: the intent was STAGED at Track B before asking.
    assert trackb.calls == [(intent, None)]


def test_business_info_partial_update_confirms():
    # All business_info fields are optional — partial updates are the norm.
    intent = make_intent("update", "business_info", {"hours": "Mon-Fri 9-6"}, 0.92)
    router = make_router(ScriptedParser(parse_intent(intent)))
    outcome = handle(router, "change my hours to 9-6")
    assert outcome.branch == "confirm"
    assert outcome.reason == "confirmation_ready"


def test_announcement_update_with_partial_fields_confirms():
    # update/delete allow partial sets: no title/body required.
    intent = make_intent("update", "announcement", {"body": "Closed July 4th"}, 0.88)
    trackb = FakeTrackB()
    router = make_router(ScriptedParser(parse_intent(intent)), trackb=trackb)
    outcome = handle(router, "update the announcement to say we're closed July 4")
    assert outcome.branch == "confirm"
    # Staged for the owner's YES/NO (Integration Phase).
    assert trackb.calls == [(intent, None)]


# ---------------------------------------------------------------- clarify


def test_missing_title_asks_one_targeted_question():
    intent = make_intent("create", "job", {"description": "cash handling"}, 0.9)
    router = make_router(ScriptedParser(parse_intent(intent)))
    outcome = handle(router, "post a job, it involves cash handling")
    assert outcome.branch == "clarify"
    assert outcome.reply_text == "What's the job title?"
    assert outcome.asked_field == "title"
    state = router.sessions.get(OWNER)
    assert state is not None and state.state == "AWAITING_CLARIFICATION"
    assert state.asked_field == "title"


def test_missing_announcement_body_asks_targeted():
    intent = make_intent("create", "announcement", {"title": "Holiday hours"}, 0.85)
    router = make_router(ScriptedParser(parse_intent(intent)))
    outcome = handle(router, "post an announcement titled holiday hours")
    assert outcome.branch == "clarify"
    assert outcome.reply_text == "What should the announcement say?"


def test_low_confidence_complete_asks_confirmation_question():
    intent = make_intent(
        "create",
        "job",
        {"title": "Cashier", "description": "evenings and weekends"},
        0.5,  # below threshold but fields complete
    )
    router = make_router(ScriptedParser(parse_intent(intent)))
    outcome = handle(router, "we need someone for the evenings, a cashier I think")
    assert outcome.branch == "clarify"
    assert "did you mean" in outcome.reply_text
    assert "Cashier" in outcome.reply_text  # targeted: references the parse


def test_no_intent_at_all_asks_to_rephrase():
    router = make_router(ScriptedParser(IntentParseResult(status="low_confidence")))
    outcome = handle(router, "blah blah")
    assert outcome.branch == "clarify"
    assert outcome.reply_text == translate("no_intent_question")


def test_image_create_without_media_asks_for_the_image():
    intent = make_intent("create", "image", {"slot": "homepage_banner"}, 0.9)
    router = make_router(ScriptedParser(parse_intent(intent)))
    outcome = handle(router, "make this the new homepage banner")
    assert outcome.branch == "clarify"
    assert outcome.reply_text == "Please send the image you'd like to use."


def test_image_delete_with_slot_confirms():
    # delete identifies the image by slot; no media needed.
    intent = make_intent("delete", "image", {"slot": "logo"}, 0.9)
    router = make_router(ScriptedParser(parse_intent(intent)))
    outcome = handle(router, "remove the old logo")
    assert outcome.branch == "confirm"


# ------------------------------------------------------- clarification loop


def test_clarification_reenters_parse_with_context_and_resolves():
    first = parse_intent(make_intent("create", "job", {"description": "cash handling"}, 0.9))
    resolved = parse_intent(
        make_intent(
            "create",
            "job",
            {"title": "Cashier", "description": "cash handling"},
            0.9,
        )
    )
    parser = ScriptedParser(first, resolved)
    router = make_router(parser)

    outcome1 = handle(router, "post a job, it involves cash handling")
    assert outcome1.branch == "clarify"
    assert outcome1.reply_text == "What's the job title?"

    outcome2 = handle(router, "Cashier")
    assert outcome2.branch == "confirm"
    assert outcome2.intent["fields"]["title"] == "Cashier"
    # The re-entry carried the prior exchange as LLM context.
    ctx = parser.calls[1]["context"]
    assert ctx is not None
    assert "post a job" in ctx
    assert "What's the job title?" in ctx
    # Loop closed: the intent is now held for confirmation (A5).
    state = router.sessions.get(OWNER)
    assert state is not None and state.state == "AWAITING_CONFIRMATION"
    assert state.pending_intent["fields"]["title"] == "Cashier"


def test_clarification_loop_caps_at_max_turns():
    low = IntentParseResult(status="low_confidence")
    parser = ScriptedParser(*([low] * (CLARIFICATION_MAX_TURNS + 1)))
    router = make_router(parser)

    for i in range(CLARIFICATION_MAX_TURNS):
        outcome = handle(router, f"attempt {i + 1}")
        assert outcome.branch == "clarify"
        assert outcome.reason != "max_turns"

    outcome = handle(router, f"attempt {CLARIFICATION_MAX_TURNS + 1}")
    assert outcome.branch == "clarify"
    assert outcome.reply_text == translate("still_unsure")
    assert outcome.reason == "max_turns"
    assert router.sessions.get(OWNER) is None


def test_mid_clarification_message_that_is_unsupported_clarifies():
    """§3: unsupported → unclear → AWAITING_CLARIFICATION (not escalate)."""
    first = parse_intent(make_intent("create", "job", {"description": "cash handling"}, 0.9))
    parser = ScriptedParser(first, IntentParseResult(status="unsupported", confidence=0.0))
    router = make_router(parser)

    assert handle(router, "post a job").branch == "clarify"
    outcome = handle(router, "actually, redesign my whole site")
    assert outcome.branch == "clarify"  # unsupported → clarify (unclear)
    state = router.sessions.get(OWNER)
    assert state is not None and state.state == "AWAITING_CLARIFICATION"


# ---------------------------------------------------------------- unclear

def test_unsupported_sends_clarification_message():
    """§3: unsupported → AWAITING_CLARIFICATION with template question (§3.4)."""
    router = make_router(ScriptedParser(IntentParseResult(status="unsupported")))
    outcome = handle(router, "redesign my homepage")
    assert outcome.branch == "clarify"
    assert "rephrase" in outcome.reply_text.lower() or "understand" in outcome.reply_text.lower()
    state = router.sessions.get(OWNER)
    assert state is not None and state.state == "AWAITING_CLARIFICATION"


def test_unsupported_rephrase_resolves_to_action():
    """Owner rephrases after unclear → clarification loop resolves."""
    intent = make_intent("create", "job", {"title": "Barista", "description": "mornings"}, 0.9)
    parser = ScriptedParser(
        IntentParseResult(status="unsupported"),
        parse_intent(intent),
    )
    router = make_router(parser)

    assert handle(router, "redesign my homepage").branch == "clarify"
    outcome = handle(router, "actually, post a job for a barista")
    assert outcome.branch == "confirm"
    assert outcome.intent is not None
    assert outcome.intent["fields"]["title"] == "Barista"
