"""Post-parse routing (A4): where does each parsed intent go?

Every `IntentParseResult` from A3 lands in exactly one of three branches:

- `confirm`  — confidence >= CONFIDENCE_THRESHOLD AND all fields required
  for that content_type/action are present. A5 takes over from here
  (confirmation messaging); we carry the intent forward, no reply yet.
- `clarify`  — confidence below threshold OR required fields missing. We
  send ONE *targeted* question (never a generic "I didn't understand"):
  the missing field when one is missing, otherwise a restatement of the
  parsed intent to confirm. The owner's reply re-enters A3 with the prior
  exchange as context (carried in `session.SessionStore` per owner_id).
- `escalate` — A3 returned the unsupported sentinel. We send the fixed
  escalation message; a "yes" logs an escalation request (owner, original
  message) that a human can pick up manually. PRD §10.

The confidence threshold is a named constant, easy to tune.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from .intent import IntentParseResult, IntentParser
from .session import SessionState, SessionStore

logger = logging.getLogger("track_a.routing")

# Confidence at or above which a parsed intent may proceed without a
# clarifying question. Tune this to trade aggressiveness vs. errors.
CONFIDENCE_THRESHOLD = 0.75

# How many clarification turns we allow before giving up and asking the
# owner to send the full request in one message.
CLARIFICATION_MAX_TURNS = 3

# ---------------------------------------------------------------------------
# Reply texts
# ---------------------------------------------------------------------------

ESCALATION_REPLY_TEXT = (
    "That's outside what I can currently handle automatically — things like "
    "job postings, announcements, and your business info (hours, contact, "
    "address, prices). For anything else, I can connect you with a developer "
    "to make the change for a small fee. Want me to do that?"
)

ESCALATION_CONFIRM_REPLY = (
    "Done — I've logged your request and someone will reach out about it. "
    "Anything else I can help with?"
)

ESCALATION_DECLINE_REPLY = (
    "No problem! If you change your mind, just ask. Anything else I can do?"
)

# No intent at all (parse failed / empty): still targeted — names what the
# bot CAN do rather than a bare "I didn't understand".
NO_INTENT_QUESTION = (
    "Sorry — I couldn't quite understand your request. You can ask me to "
    "post a job, add an announcement, or update your business info like "
    "hours, contact, address, or prices. Could you rephrase it?"
)

# After CLARIFICATION_MAX_TURNS of unresolved back-and-forth.
STILL_UNSURE_REPLY_TEXT = (
    "I'm having trouble understanding what you'd like to change. Could you "
    "text me the request in one message, for example: \"post a job for a "
    "part-time barista\" or \"change my hours to 9-6\"?"
)

# ---------------------------------------------------------------------------
# Required-field knowledge (mirrors shared-contract/intent.schema.json)
# ---------------------------------------------------------------------------

# Fields required on `create` per content_type. update/delete accept
# partial sets. business_info is all-optional by design.
_REQUIRED_ON_CREATE: dict[str, tuple[str, ...]] = {
    "job": ("title", "description"),
    "announcement": ("title", "body"),
    "business_info": (),
    "image": ("slot",),
}

# The ONE targeted question to ask per missing field. business_info fields
# never gate confirmation, but they can still be missing from a low-faith
# parse, so give them questions too.
FIELD_QUESTIONS: dict[str, str] = {
    "job.title": "What's the job title?",
    "job.description": "Can you describe the job?",
    "job.location": "Where is the job located?",
    "job.remote": "Is the role remote, on-site, or hybrid?",
    "job.category": "What category is the job?",
    "announcement.title": "What should the announcement be titled?",
    "announcement.body": "What should the announcement say?",
    "image.slot": "Where should the image go — homepage banner, logo, or gallery?",
    "image.media_url": "Please send the image you'd like to use.",
    "business_info.phone": "What phone number should I update to?",
    "business_info.hours": "What are the new opening hours?",
    "business_info.address": "What's the address?",
    "business_info.prices": "What are the new prices?",
}


def missing_required_fields(intent: dict[str, Any]) -> list[str]:
    """Fields a validated intent still needs before it can be confirmed.

    Mirrors the contract's allOf rules: job/announcement require
    title+description / title+body on `create` only; business_info is
    always partial; image always needs `slot`, plus exactly one of
    media_url / media_base64 unless the action is delete.
    """
    content_type = intent.get("content_type")
    action = intent.get("action")
    fields = intent.get("fields") or {}

    if content_type == "image":
        missing: list[str] = []
        if not fields.get("slot"):
            missing.append("slot")
        if action != "delete" and not fields.get("media_url") and not fields.get(
            "media_base64"
        ):
            # No separate field name; represent "the image itself" by the
            # media_url slot so the question reads naturally.
            missing.append("media_url")
        return missing

    if action != "create":
        return []  # update/delete accept partial sets

    return [
        name
        for name in _REQUIRED_ON_CREATE.get(content_type or "", ())
        if not fields.get(name)
    ]


def targeted_question(content_type: str | None, field: str | None) -> str:
    """One targeted clarifying question for the gap, never a generic one."""
    if content_type and field:
        question = FIELD_QUESTIONS.get(f"{content_type}.{field}")
        if question:
            return question
        return f"Could you tell me the {field}?"
    return NO_INTENT_QUESTION


def _is_yes(text: str) -> bool:
    normalized = re.sub(r"[^a-z ]", "", (text or "").strip().lower())
    return normalized in {
        "yes", "yep", "yeah", "y", "sure", "ok", "okay", "do it", "please",
        "yes please", "please do", "please do it", "go ahead", "go for it",
        "sounds good", "correct", "thats right", "absolutely", "for sure",
        "yeah do it", "yes do it", "do that", "sure do",
    } or normalized.startswith("yes")


def _is_no(text: str) -> bool:
    normalized = re.sub(r"[^a-z ]", "", (text or "").strip().lower())
    return normalized in {
        "no", "nope", "nah", "not now", "no thanks", "no thank you", "n",
        "never mind", "skip it", "forget it", "no way", "not really",
    } or normalized.startswith("no ")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@dataclass
class RouteOutcome:
    branch: str  # "confirm" | "clarify" | "escalate"
    reply_text: str | None = None
    intent: dict[str, Any] | None = None  # confirmation-ready intent (A5)
    asked_field: str | None = None
    reason: str = ""


def _intent_summary(intent: dict[str, Any]) -> str:
    """Short human summary of a parsed intent, for confirmation questions."""
    action = intent.get("action", "change")
    content_type = intent.get("content_type", "")
    fields = intent.get("fields") or {}
    verb = {"create": "post", "update": "update", "delete": "remove"}.get(
        action, "change"
    )

    if content_type == "job" and fields.get("title"):
        return f"{verb} a job titled '{fields['title']}'"
    if content_type == "announcement" and fields.get("title"):
        return f"{verb} an announcement titled '{fields['title']}'"
    if content_type == "business_info" and fields:
        return f"{verb} your business info ({', '.join(fields)})"
    if content_type == "image" and fields.get("slot"):
        return f"{verb} your {fields['slot'].replace('_', ' ')} image"
    if content_type == "job":
        return "change a job posting"
    if content_type == "announcement":
        return "post an announcement"
    if content_type == "business_info":
        return "update your business info"
    return "make a change to your site"


def _confirmation_question(intent: dict[str, Any]) -> str:
    """Targeted question when confidence is low but fields are complete."""
    return (
        f"Just to double-check — did you mean: {_intent_summary(intent)}? "
        "Reply 'yes' or tell me what to change."
    )


class IntentRouter:
    """Decides the branch for a parse result and drives the loops.

    `handle_message` is the entrypoint the webhook will call: it checks
    the owner's session (pending clarification / escalation), re-enters
    A3 parsing with conversation context when mid-clarification, routes
    the result, and persists session state.

    The escalation logger is injected (a callable taking owner_id and the
    original message) so the router stays independent of the store; the
    app wires it to `store.log_escalation_request`.
    """

    def __init__(
        self,
        parser: IntentParser | None = None,
        sessions: SessionStore | None = None,
        *,
        threshold: float = CONFIDENCE_THRESHOLD,
        log_escalation: Callable[[str, str], None] | None = None,
    ) -> None:
        self.parser = parser or IntentParser()
        self.sessions = sessions or SessionStore()
        self.threshold = threshold
        self.log_escalation = log_escalation or (lambda owner, msg: None)

    async def handle_message(self, owner_id: str, message_text: str) -> RouteOutcome:
        """Route one owner message to exactly one branch."""
        message_text = (message_text or "").strip()
        state = self.sessions.get(owner_id)

        # --- mid-escalation: awaiting a yes/no on the developer offer ---
        if state is not None and state.branch == "escalate":
            return self._handle_escalation_reply(owner_id, state, message_text)

        # --- normal path: parse (with prior-exchange context if clarifying) ---
        context = (
            _format_exchange(state)
            if state is not None and state.branch == "clarify"
            else None
        )
        parse = await self.parser.parse(message_text, owner_id, context=context)
        return self._route(owner_id, parse, message_text, prior=state)

    # -- branch decisions --------------------------------------------------

    def _route(
        self,
        owner_id: str,
        parse: IntentParseResult,
        original_message: str,
        prior: SessionState | None,
    ) -> RouteOutcome:
        if parse.status == "unsupported":
            self.sessions.set(
                owner_id,
                SessionState(
                    branch="escalate",
                    original_message=original_message,
                    exchange=_exchange(prior, original_message),
                ),
            )
            return RouteOutcome(
                branch="escalate",
                reply_text=ESCALATION_REPLY_TEXT,
                reason="unsupported",
            )

        if parse.status == "low_confidence" or parse.intent is None:
            return self._clarify(
                owner_id, prior, asked_field=None, intent=None,
                original_message=original_message,
                reason="low_confidence_no_intent",
            )

        intent = parse.intent
        missing = missing_required_fields(intent)
        if parse.confidence >= self.threshold and not missing:
            # Confirmation-ready: hand the intent to A5, no reply yet.
            self.sessions.clear(owner_id)
            return RouteOutcome(
                branch="confirm", intent=intent, reason="confirmation_ready"
            )

        if missing:
            return self._clarify(
                owner_id, prior, asked_field=missing[0], intent=intent,
                original_message=original_message,
                reason=f"missing_field:{missing[0]}",
            )
        return self._clarify(
            owner_id, prior, asked_field=None, intent=intent,
            original_message=original_message,
            reason="low_confidence",
        )

    def _clarify(
        self,
        owner_id: str,
        prior: SessionState | None,
        *,
        asked_field: str | None,
        intent: dict[str, Any] | None,
        original_message: str,
        reason: str,
    ) -> RouteOutcome:
        turns = (
            prior.turns if prior is not None and prior.branch == "clarify" else 0
        ) + 1
        if turns > CLARIFICATION_MAX_TURNS:
            self.sessions.clear(owner_id)
            return RouteOutcome(
                branch="clarify",
                reply_text=STILL_UNSURE_REPLY_TEXT,
                reason="max_turns",
            )

        # Transcript: prior turns (owner messages + our questions), then the
        # message that just arrived. The question is appended once we know it.
        exchange = _exchange(prior, original_message)
        if asked_field:
            question = targeted_question(
                intent.get("content_type") if intent else None, asked_field
            )
        elif intent is not None:
            question = _confirmation_question(intent)
        else:
            question = NO_INTENT_QUESTION
        exchange.append({"role": "assistant", "text": question})

        self.sessions.set(
            owner_id,
            SessionState(
                branch="clarify",
                pending_intent=intent,
                asked_field=asked_field,
                turns=turns,
                exchange=exchange,
            ),
        )
        return RouteOutcome(
            branch="clarify",
            reply_text=question,
            asked_field=asked_field,
            reason=reason,
        )

    def _handle_escalation_reply(
        self, owner_id: str, state: SessionState, message_text: str
    ) -> RouteOutcome:
        if _is_yes(message_text):
            original = state.original_message or ""
            self.log_escalation(owner_id, original)
            logger.info(
                "escalation request logged for owner %s: %r", owner_id, original[:80]
            )
            self.sessions.clear(owner_id)
            return RouteOutcome(
                branch="escalate",
                reply_text=ESCALATION_CONFIRM_REPLY,
                reason="escalation_logged",
            )
        if _is_no(message_text):
            self.sessions.clear(owner_id)
            return RouteOutcome(
                branch="escalate",
                reply_text=ESCALATION_DECLINE_REPLY,
                reason="escalation_declined",
            )
        # Anything else: still waiting for a clear yes/no.
        return RouteOutcome(
            branch="escalate",
            reply_text=ESCALATION_REPLY_TEXT,
            reason="escalation_pending",
        )


# ---------------------------------------------------------------------------
# Exchange / context helpers
# ---------------------------------------------------------------------------


def _exchange(
    prior: SessionState | None, latest_owner_message: str | None
) -> list[dict[str, str]]:
    """Return the bounded transcript, optionally appending the new message.

    Keeps only the last few turns so the LLM context stays short.
    """
    turns = list(prior.exchange) if prior is not None else []
    if latest_owner_message:
        turns.append({"role": "owner", "text": latest_owner_message})
    return turns[-6:]


def _format_exchange(state: SessionState) -> str:
    """Render the prior exchange as context for the next A3 parse call."""
    lines = ["Previous exchange with this owner:"]
    for turn in state.exchange:
        speaker = "Owner" if turn.get("role") == "owner" else "Assistant"
        lines.append(f"{speaker}: {turn.get('text', '')}")
    return "\n".join(lines)
