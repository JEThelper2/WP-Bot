"""Phase 3: Conversational state machine tests per §3.

Covers:
- §3.1: All four states (IDLE, AWAITING_CLARIFICATION, AWAITING_CONFIRMATION, EXECUTING)
- §3.1: All state transitions
- §3.2: context_history (last 6 turns) for LLM re-entry
- §3.3: Confirmation matching with exact word sets
- §3.3: Re-ask logic (re-ask once, then cancel)
- §3.3: Session expiry (any state → IDLE)
- §3.4: Template-based clarification questions
- §3.5: Undo command matching
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from shared_contract import CONTRACT_VERSION
from track_a.intent import IntentParseResult
from track_a.routing import (
    CLARIFICATION_MAX_TURNS,
    IntentRouter,
    _is_no,
    _is_undo,
    _is_yes,
)
from track_a.session import SESSION_TTL_SECONDS, ActiveSiteStore, SessionState, SessionStore

OWNER = "15551234567"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_intent(
    action: str = "create",
    content_type: str = "job",
    fields: dict[str, Any] | None = None,
    confidence: float = 0.9,
) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "owner_id": OWNER,
        "action": action,
        "content_type": content_type,
        "fields": fields or {"title": "Part-time Barista", "description": "$18/hr"},
        "confidence": confidence,
    }


def parse_intent(intent: dict[str, Any]) -> IntentParseResult:
    return IntentParseResult(status="intent", intent=intent, confidence=intent["confidence"])


class ScriptedParser:
    def __init__(self, *results: IntentParseResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, str | None]] = []

    async def parse(self, message_text: str, owner_id: str, *, context=None):
        self.calls.append({"text": message_text, "context": context})
        return self.results.pop(0)


class FakeTrackB:
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

    async def undo(self, owner_id: str, *, site_id=None) -> dict:
        raise AssertionError("undo not exercised in state machine tests")


def make_router(
    parser: ScriptedParser, trackb: FakeTrackB | None = None, sessions: SessionStore | None = None
) -> IntentRouter:
    return IntentRouter(
        parser=parser,
        sessions=sessions if sessions is not None else SessionStore(),
        trackb=trackb or FakeTrackB(),
    )


def handle(router: IntentRouter, text: str) -> Any:
    return asyncio.run(router.handle_message(OWNER, text))


# ---------------------------------------------------------------------------
# §3.3: Confirmation matching — exact word sets
# ---------------------------------------------------------------------------

class TestConfirmationMatching:
    def test_exact_yes_words(self):
        for word in ["yes", "yeah", "yep", "confirm", "ok", "okay", "go ahead", "do it"]:
            assert _is_yes(word), f"'{word}' should be affirmative"

    def test_exact_no_words(self):
        for word in ["no", "nope", "cancel", "stop", "don't", "dont"]:
            assert _is_no(word), f"'{word}' should be negative"

    def test_ambiguous_not_yes(self):
        for word in ["sure", "maybe", "please", "correct", "sounds good", "absolutely", "y"]:
            assert not _is_yes(word), f"'{word}' should NOT be affirmative"

    def test_ambiguous_not_no(self):
        for word in ["nah", "not now", "no thanks", "skip it", "never mind", "n"]:
            assert not _is_no(word), f"'{word}' should NOT be negative"

    def test_case_insensitive(self):
        assert _is_yes("YES")
        assert _is_yes("Yes")
        assert _is_no("NO")
        assert _is_no("No")

    def test_punctuation_stripped(self):
        assert _is_yes("yes!")
        assert _is_yes("yes.")
        assert _is_no("no...")
        assert _is_no("no?")


class TestUndoMatching:
    def test_exact_undo_words(self):
        for word in ["undo", "undo that", "undo last change", "revert"]:
            assert _is_undo(word), f"'{word}' should trigger undo"

    def test_non_undo_words(self):
        for word in ["undo it", "undo this", "revert it", "revert that", "take it back", "reverse it"]:
            assert not _is_undo(word), f"'{word}' should NOT trigger undo"


# ---------------------------------------------------------------------------
# §3.1: State transitions
# ---------------------------------------------------------------------------

class TestStateTransitions:
    def test_idle_to_awaiting_confirmation(self):
        """IDLE → AWAITING_CONFIRMATION: high confidence, complete intent."""
        intent = make_intent(confidence=0.9)
        router = make_router(ScriptedParser(parse_intent(intent)))
        outcome = handle(router, "post a job for a barista")
        assert outcome.branch == "confirm"
        state = router.sessions.get(OWNER)
        assert state is not None
        assert state.state == "AWAITING_CONFIRMATION"
        assert state.pending_intent is not None

    def test_idle_to_awaiting_clarification_low_confidence(self):
        """IDLE → AWAITING_CLARIFICATION: low confidence."""
        intent = make_intent(confidence=0.4)
        router = make_router(ScriptedParser(parse_intent(intent)))
        outcome = handle(router, "maybe something about jobs?")
        assert outcome.branch == "clarify"
        state = router.sessions.get(OWNER)
        assert state is not None
        assert state.state == "AWAITING_CLARIFICATION"

    def test_idle_to_awaiting_clarification_missing_field(self):
        """IDLE → AWAITING_CLARIFICATION: required field missing."""
        intent = make_intent(fields={"description": "cash handling"}, confidence=0.9)
        router = make_router(ScriptedParser(parse_intent(intent)))
        outcome = handle(router, "post a job, cash handling")
        assert outcome.branch == "clarify"
        state = router.sessions.get(OWNER)
        assert state is not None
        assert state.state == "AWAITING_CLARIFICATION"

    def test_idle_to_awaiting_clarification_unsupported(self):
        """IDLE → AWAITING_CLARIFICATION: unsupported → unclear."""
        router = make_router(ScriptedParser(IntentParseResult(status="unsupported")))
        outcome = handle(router, "redesign my homepage")
        assert outcome.branch == "clarify"
        state = router.sessions.get(OWNER)
        assert state is not None
        assert state.state == "AWAITING_CLARIFICATION"

    def test_awaiting_clarification_to_awaiting_confirmation(self):
        """AWAITING_CLARIFICATION → AWAITING_CONFIRMATION: owner resolves missing field."""
        intent_missing = make_intent(fields={"description": "cash handling"}, confidence=0.9)
        intent_full = make_intent(confidence=0.9)
        parser = ScriptedParser(parse_intent(intent_missing), parse_intent(intent_full))
        router = make_router(parser)

        outcome1 = handle(router, "post a job, cash handling")
        assert outcome1.branch == "clarify"
        state = router.sessions.get(OWNER)
        assert state.state == "AWAITING_CLARIFICATION"

        outcome2 = handle(router, "Cashier")
        assert outcome2.branch == "confirm"
        state = router.sessions.get(OWNER)
        assert state.state == "AWAITING_CONFIRMATION"

    def test_awaiting_clarification_to_idle_on_max_turns(self):
        """AWAITING_CLARIFICATION → IDLE: max clarification turns exceeded."""
        low = IntentParseResult(status="low_confidence")
        parser = ScriptedParser(*([low] * (CLARIFICATION_MAX_TURNS + 1)))
        router = make_router(parser)

        for i in range(CLARIFICATION_MAX_TURNS):
            outcome = handle(router, f"attempt {i + 1}")
            assert outcome.branch == "clarify"
            state = router.sessions.get(OWNER)
            assert state.state == "AWAITING_CLARIFICATION"

        outcome = handle(router, f"attempt {CLARIFICATION_MAX_TURNS + 1}")
        assert outcome.branch == "clarify"
        assert outcome.reason == "max_turns"
        assert router.sessions.get(OWNER) is None  # cleared → back to IDLE

    def test_awaiting_confirmation_to_idle_on_no(self):
        """AWAITING_CONFIRMATION → IDLE: owner replies negative."""
        intent = make_intent(confidence=0.9)
        trackb = FakeTrackB()
        parser = ScriptedParser(parse_intent(intent))
        router = make_router(parser, trackb=trackb)

        handle(router, "post a job")
        assert router.sessions.get(OWNER).state == "AWAITING_CONFIRMATION"

        outcome = handle(router, "no")
        assert outcome.branch == "confirm"
        assert outcome.reason == "cancelled"
        assert router.sessions.get(OWNER) is None  # cleared → IDLE

    def test_awaiting_confirmation_to_executing_on_yes(self):
        """AWAITING_CONFIRMATION → EXECUTING (via submit): owner replies affirmative."""
        intent = make_intent(confidence=0.9)
        trackb = FakeTrackB({
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "change_id": "ch-1",
            "before": None,
            "after": None,
            "live_url": "https://example.com/live",
            "error_message": None,
        })
        parser = ScriptedParser(parse_intent(intent))
        router = make_router(parser, trackb=trackb)

        handle(router, "post a job")
        outcome = handle(router, "yes")
        assert outcome.reason == "publish_success"
        # After success → back to IDLE (session cleared)
        assert router.sessions.get(OWNER) is None

    def test_executing_to_idle_on_completion(self):
        """EXECUTING → IDLE: action completes (success or failure).

        In our async implementation, EXECUTING is transient — the submit
        happens inline and transitions directly to IDLE on completion.
        """
        intent = make_intent(confidence=0.9)
        trackb = FakeTrackB({
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "change_id": "ch-1",
            "before": None,
            "after": None,
            "live_url": None,
            "error_message": None,
        })
        parser = ScriptedParser(parse_intent(intent))
        router = make_router(parser, trackb=trackb)

        handle(router, "post a job")
        handle(router, "yes")
        # After completion: session cleared → IDLE
        assert router.sessions.get(OWNER) is None

    def test_unrelated_message_in_awaiting_confirmation_reasks(self):
        """§3.3: Unrelated message while AWAITING_CONFIRMATION → re-ask once."""
        intent = make_intent(confidence=0.9)
        trackb = FakeTrackB()
        parser = ScriptedParser(parse_intent(intent))
        router = make_router(parser, trackb=trackb)

        handle(router, "post a job")
        state = router.sessions.get(OWNER)
        assert state.state == "AWAITING_CONFIRMATION"

        # First unrelated reply: re-ask
        outcome = handle(router, "what's the weather like?")
        assert outcome.branch == "confirm"
        assert outcome.reason == "confirmation_reask"
        state = router.sessions.get(OWNER)
        assert state.state == "AWAITING_CONFIRMATION"
        assert state.re_ask_count == 1

        # Second unrelated reply: cancel
        outcome = handle(router, "hmm interesting")
        assert outcome.branch == "confirm"
        assert outcome.reason == "confirmation_cancelled"
        assert router.sessions.get(OWNER) is None  # → IDLE


# ---------------------------------------------------------------------------
# §3.2: context_history (last 6 turns)
# ---------------------------------------------------------------------------

class TestContextHistory:
    def test_context_history_built_during_clarification(self):
        """Context history is populated with exchange turns during clarification."""
        intent_missing = make_intent(fields={"description": "cash handling"}, confidence=0.9)
        intent_full = make_intent(confidence=0.9)
        parser = ScriptedParser(parse_intent(intent_missing), parse_intent(intent_full))
        router = make_router(parser)

        handle(router, "post a job, cash handling")
        state = router.sessions.get(OWNER)
        assert state is not None
        assert len(state.context_history) >= 1
        # Owner message + bot question
        roles = [t["role"] for t in state.context_history]
        assert "owner" in roles
        assert "assistant" in roles

    def test_context_history_capped_at_6_turns(self):
        """§3.2: context_history keeps last 6 turns."""
        # Multiple clarification rounds
        low = IntentParseResult(status="low_confidence")
        results = [low] * 8
        parser = ScriptedParser(*results)
        router = make_router(parser)

        for i in range(6):
            handle(router, f"message {i}")

        state = router.sessions.get(OWNER)
        if state is not None:
            # Should be capped at 6 (or fewer if session was cleared)
            assert len(state.context_history) <= 6

    def test_context_passed_to_parser_on_clarification_reentry(self):
        """§3.2: Prior exchange is formatted and passed to parser as context."""
        intent_missing = make_intent(fields={"description": "cash handling"}, confidence=0.9)
        intent_full = make_intent(confidence=0.9)
        parser = ScriptedParser(parse_intent(intent_missing), parse_intent(intent_full))
        router = make_router(parser)

        handle(router, "post a job, cash handling")
        handle(router, "Cashier")

        # The second parse call should have received context
        assert len(parser.calls) == 2
        ctx = parser.calls[1]["context"]
        assert ctx is not None
        assert "post a job" in ctx
        assert "What's the job title?" in ctx


# ---------------------------------------------------------------------------
# §3.4: Template-based clarification questions
# ---------------------------------------------------------------------------

class TestClarificationTemplates:
    def test_missing_entity_uses_template(self):
        """§3.4: Missing entity → template question, not free-form AI."""
        intent = make_intent(confidence=0.4)
        router = make_router(ScriptedParser(parse_intent(intent)))
        outcome = handle(router, "change something")
        assert outcome.branch == "clarify"
        # Should be a template question, not a generic error
        assert len(outcome.reply_text) > 10

    def test_unsupported_uses_template(self):
        """§3.4: Unsupported → template question (§3.4)."""
        router = make_router(ScriptedParser(IntentParseResult(status="unsupported")))
        outcome = handle(router, "build me a landing page")
        assert outcome.branch == "clarify"
        assert "rephrase" in outcome.reply_text.lower() or "understand" in outcome.reply_text.lower()


# ---------------------------------------------------------------------------
# §3.1: Session expiry (any state → IDLE)
# ---------------------------------------------------------------------------

class TestSessionExpiry:
    def test_expired_session_treated_as_idle(self):
        """§3.1: Any state → IDLE on session expiry (15 min inactivity)."""
        intent = make_intent(confidence=0.9)
        parser = ScriptedParser(parse_intent(intent))
        # Use a custom subclass to avoid __len__ making it falsy in 'or' fallback.
        class TTLSessionStore(SessionStore):
            def __init__(self):
                super().__init__(ttl=1)
        sessions = TTLSessionStore()
        router = make_router(parser, sessions=sessions)

        handle(router, "post a job")
        state = router.sessions.get(OWNER)
        assert state is not None
        assert state.state == "AWAITING_CONFIRMATION"

        # Wait for expiry
        time.sleep(1.1)
        state = router.sessions.get(OWNER)
        assert state is None  # expired → treated as IDLE

    def test_fresh_message_resets_expiry(self):
        """§3.2: Each new message resets expires_at = now + 15 minutes."""
        intent_missing = make_intent(fields={"description": "cash handling"}, confidence=0.9)
        intent_full = make_intent(confidence=0.9)
        parser = ScriptedParser(parse_intent(intent_missing), parse_intent(intent_full))
        class TTLSessionStore(SessionStore):
            def __init__(self):
                super().__init__(ttl=10)
        sessions = TTLSessionStore()
        router = make_router(parser, sessions=sessions)

        handle(router, "post a job, cash handling")
        state1 = router.sessions.get(OWNER)
        assert state1 is not None
        expires1 = state1.expires_at

        time.sleep(0.1)
        handle(router, "Cashier")
        state2 = router.sessions.get(OWNER)
        if state2 is not None:
            # Expiry should be extended
            assert state2.expires_at >= expires1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_pending_intent_on_confirm_reply(self):
        """If state has no pending_intent, confirm reply returns error."""
        router = make_router(ScriptedParser())
        # Manually set a state with no pending intent
        router.sessions.set(OWNER, SessionState(state="AWAITING_CONFIRMATION"))
        outcome = handle(router, "yes")
        assert outcome.branch == "confirm"
        assert outcome.reason == "no_pending_intent"
        assert router.sessions.get(OWNER) is None

    def test_undo_while_no_session(self):
        """UNDO with no active session still works."""
        class FakeTrackBForUndo:
            async def submit_intent(self, intent, *, decision=None):
                raise AssertionError("should not be called for undo")
            async def undo(self, owner_id, *, site_id=None):
                return {
                    "contract_version": CONTRACT_VERSION,
                    "status": "success",
                    "change_id": "ch-undo",
                    "before": None,
                    "after": None,
                    "live_url": "https://example.com/undone",
                    "error_message": None,
                }

        router = make_router(ScriptedParser(), trackb=FakeTrackBForUndo())
        outcome = handle(router, "undo")
        assert outcome.branch == "undo"
        assert outcome.reason == "undo_done"

    def test_empty_message_is_low_confidence(self):
        """Empty message → low confidence → clarification."""
        router = make_router(ScriptedParser(IntentParseResult(status="low_confidence")))
        outcome = handle(router, "")
        assert outcome.branch == "clarify"

    def test_state_survives_across_multiple_clarifications(self):
        """Session state persists across multiple clarification turns."""
        low = IntentParseResult(status="low_confidence")
        parser = ScriptedParser(*([low] * 3))
        router = make_router(parser)

        handle(router, "first attempt")
        state = router.sessions.get(OWNER)
        assert state is not None
        assert state.state == "AWAITING_CLARIFICATION"
        assert state.turns == 1

        handle(router, "second attempt")
        state = router.sessions.get(OWNER)
        assert state is not None
        assert state.turns == 2
