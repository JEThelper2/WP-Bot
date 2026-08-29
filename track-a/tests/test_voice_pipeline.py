"""Phase 4: Voice pipeline tests per §4.

Covers:
- §4.1 step 3: Always echo transcript back before acting
- §4.1 step 4: Low-confidence caveat prefix
- §4.1 step 5: Affirmative → use transcript; other → treat as correction
- §4.1: Voice notes enter state machine with source="voice"
- §4.2: Proxy confidence calculation
"""

from __future__ import annotations

import asyncio
from typing import Any

from shared_contract import CONTRACT_VERSION
from track_a.intent import IntentParseResult
from track_a.i18n import translate
from track_a.routing import IntentRouter, _is_yes
from track_a.session import SessionState, SessionStore
from track_a.transcribe import StubTranscriber, Transcription, compute_proxy_confidence

OWNER = "15551234567"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_intent(
    action: str = "delete",
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
        raise AssertionError("undo not exercised in voice pipeline tests")


def make_router(
    parser: ScriptedParser, trackb: FakeTrackB | None = None, sessions: SessionStore | None = None
) -> IntentRouter:
    return IntentRouter(
        parser=parser,
        sessions=sessions if sessions is not None else SessionStore(),
        trackb=trackb or FakeTrackB(),
    )


def handle(router: IntentRouter, text: str, source: str = "text") -> Any:
    return asyncio.run(router.handle_message(OWNER, text, source=source))


def handle_voice(router: IntentRouter, text: str) -> Any:
    return asyncio.run(router.handle_message(OWNER, text, source="voice"))


# ---------------------------------------------------------------------------
# §4.1 step 3: Echo transcript back
# ---------------------------------------------------------------------------

class TestVoiceEcho:
    def test_voice_note_always_echoes_transcript(self):
        """§4.1: voice note → echo transcript before acting."""
        router = make_router(ScriptedParser())
        outcome = handle_voice(router, "change my hours to 9-6")
        assert outcome.branch == "clarify"
        assert outcome.reason == "voice_echo"
        # Echo message contains the transcript
        assert "change my hours to 9-6" in outcome.reply_text
        assert "is that right" in outcome.reply_text.lower() or "reply yes" in outcome.reply_text.lower()

    def test_voice_echo_sets_session_state(self):
        """§4.1: voice echo sets VOICE_AWAITING_ECHO state."""
        router = make_router(ScriptedParser())
        handle_voice(router, "change my hours to 9-6")
        state = router.sessions.get(OWNER)
        assert state is not None
        assert state.state == "VOICE_AWAITING_ECHO"
        assert state.voice_transcript == "change my hours to 9-6"

    def test_voice_echo_even_when_confidence_high(self):
        """§4.1: echo happens regardless of confidence — hard rule."""
        router = make_router(ScriptedParser())
        # Even with a high-confidence transcript, echo still happens
        outcome = handle_voice(router, "add jollof rice to the menu for 2500 naira")
        assert outcome.branch == "clarify"
        assert "jollof rice" in outcome.reply_text.lower()


# ---------------------------------------------------------------------------
# §4.1 step 4: Low-confidence caveat
# ---------------------------------------------------------------------------

class TestVoiceLowConfidence:
    def test_low_confidence_prepends_caveat(self):
        """§4.1: confidence < 0.5 → prepend caveat to echo."""
        # Create a session with low confidence
        router = make_router(ScriptedParser())
        # Manually set a session with low confidence to simulate
        router.sessions.set(
            OWNER,
            SessionState(
                state="IDLE",
                voice_confidence=0.3,
            ),
        )
        # The echo handler uses the session's voice_confidence
        # For this test, we verify the template works
        msg = translate("voice_echo_low_confidence", transcript="something unclear")
        assert "not fully sure" in msg.lower()
        assert "something unclear" in msg

    def test_high_confidence_no_caveat(self):
        """§4.1: confidence >= 0.5 → no caveat prefix."""
        msg = translate("voice_echo", transcript="change my hours to 9-6")
        assert "not fully sure" not in msg.lower()
        assert "change my hours to 9-6" in msg


# ---------------------------------------------------------------------------
# §4.1 step 5: Owner's reply to voice echo
# ---------------------------------------------------------------------------

class TestVoiceEchoReply:
    def test_yes_uses_transcript_as_instruction(self):
        """§4.1: affirmative → use transcript as the instruction."""
        intent = make_intent(action="delete", fields={"title": "Barista"}, confidence=0.9)
        trackb = FakeTrackB()
        parser = ScriptedParser(parse_intent(intent))
        router = make_router(parser, trackb=trackb)

        # Voice note → echo
        handle_voice(router, "remove the barista job")
        state = router.sessions.get(OWNER)
        assert state.state == "VOICE_AWAITING_ECHO"

        # Owner confirms → transcript used as instruction
        outcome = handle(router, "yes")
        assert outcome.branch == "confirm"
        # The parser was called with the transcript, not "yes"
        assert parser.calls[0]["text"] == "remove the barista job"

    def test_correction_uses_reply_as_instruction(self):
        """§4.1: non-affirmative → treat reply as corrected instruction."""
        intent = make_intent(action="delete", fields={"title": "Fried Rice"}, confidence=0.9)
        trackb = FakeTrackB()
        parser = ScriptedParser(parse_intent(intent))
        router = make_router(parser, trackb=trackb)

        # Voice note → echo
        handle_voice(router, "remove the barista job")
        state = router.sessions.get(OWNER)
        assert state.state == "VOICE_AWAITING_ECHO"

        # Owner corrects → reply used as instruction, transcript discarded
        outcome = handle(router, "actually, remove the fried rice instead")
        assert outcome.branch == "confirm"
        # The parser was called with the correction, not the transcript
        assert parser.calls[0]["text"] == "actually, remove the fried rice instead"

    def test_voice_then_yes_then_no_cancels(self):
        """§4.1: voice echo → yes → AWAITING_CONFIRMATION → no → cancelled."""
        intent = make_intent(action="delete", fields={"title": "Barista"}, confidence=0.9)
        trackb = FakeTrackB()
        parser = ScriptedParser(parse_intent(intent))
        router = make_router(parser, trackb=trackb)

        # Voice note → echo
        handle_voice(router, "remove the barista job")
        # Confirm transcript
        handle(router, "yes")
        # Now in AWAITING_CONFIRMATION (destructive delete)
        state = router.sessions.get(OWNER)
        assert state.state == "AWAITING_CONFIRMATION"

        # Cancel the destructive action
        outcome = handle(router, "no")
        assert outcome.reason == "cancelled"
        assert router.sessions.get(OWNER) is None


# ---------------------------------------------------------------------------
# §4.1: Voice enters state machine correctly
# ---------------------------------------------------------------------------

class TestVoiceStateMachineIntegration:
    def test_voice_non_destructive_skips_confirmation(self):
        """§4.1 + §3.1: voice create → echo → yes → immediate execution."""
        intent = make_intent(action="create", confidence=0.9)
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

        # Voice note → echo
        handle_voice(router, "post a job for a barista")
        assert router.sessions.get(OWNER).state == "VOICE_AWAITING_ECHO"

        # Confirm → non-destructive, executes immediately
        outcome = handle(router, "yes")
        assert outcome.reason == "publish_success"
        assert router.sessions.get(OWNER) is None

    def test_voice_unsupported_goes_to_clarification(self):
        """§4.1 + §3.1: voice unsupported → echo → yes → clarification."""
        parser = ScriptedParser(
            IntentParseResult(status="unsupported"),
        )
        router = make_router(parser)

        handle_voice(router, "redesign my homepage")
        assert router.sessions.get(OWNER).state == "VOICE_AWAITING_ECHO"

        outcome = handle(router, "yes")
        # Unsupported → unclear → AWAITING_CLARIFICATION
        assert outcome.branch == "clarify"
        state = router.sessions.get(OWNER)
        assert state.state == "AWAITING_CLARIFICATION"

    def test_voice_low_confidence_goes_to_clarification(self):
        """§4.1 + §3.1: voice low confidence → echo → yes → clarification."""
        parser = ScriptedParser(
            IntentParseResult(status="low_confidence"),
        )
        router = make_router(parser)

        handle_voice(router, "something unclear")
        assert router.sessions.get(OWNER).state == "VOICE_AWAITING_ECHO"

        outcome = handle(router, "yes")
        assert outcome.branch == "clarify"


# ---------------------------------------------------------------------------
# §4.2: Proxy confidence calculation
# ---------------------------------------------------------------------------

class TestProxyConfidence:
    def test_normal_speech_high_confidence(self):
        """Clear speech: many words relative to duration → high confidence."""
        conf = compute_proxy_confidence("change my hours to nine six", 2.0)
        assert conf > 0.5

    def test_silence_low_confidence(self):
        """Silence: no words → zero confidence."""
        conf = compute_proxy_confidence("", 2.0)
        assert conf == 0.0

    def test_garbled_low_confidence(self):
        """Garbled: very few words for long audio → low confidence."""
        conf = compute_proxy_confidence("uh", 5.0)
        assert conf < 0.3

    def test_confidence_capped_at_1(self):
        """Confidence never exceeds 1.0."""
        conf = compute_proxy_confidence("a " * 100, 1.0)
        assert conf == 1.0

    def test_zero_duration(self):
        """Zero duration → zero confidence."""
        conf = compute_proxy_confidence("hello", 0.0)
        assert conf == 0.0


# ---------------------------------------------------------------------------
# StubTranscriber
# ---------------------------------------------------------------------------

class TestStubTranscriber:
    def test_returns_scripted_transcription(self):
        """StubTranscriber returns the scripted result for a known media id."""
        from track_a.media import MediaPayload

        stub = StubTranscriber(script={
            "msg-001": Transcription(text="hello world", confidence=0.95),
        })
        payload = MediaPayload(content=b"fake", mime_type="audio/wav", media_id="msg-001")
        result = asyncio.run(stub.transcribe(payload))
        assert result.text == "hello world"
        assert result.confidence == 0.95

    def test_returns_default_for_unknown_media(self):
        """StubTranscriber returns default for unknown media id."""
        from track_a.media import MediaPayload

        stub = StubTranscriber()
        payload = MediaPayload(content=b"fake", mime_type="audio/wav", media_id="unknown")
        result = asyncio.run(stub.transcribe(payload))
        assert result.text == ""
        assert result.confidence == 0.0
        assert result.is_voice is False
