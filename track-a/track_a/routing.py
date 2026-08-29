"""Post-parse routing (A4): where does each parsed intent go?

Every `IntentParseResult` from A3 lands in exactly one of the four
states from PRODUCTION_SPEC_DETAILED.md §3.1:

- ``IDLE`` — no pending action; next message parsed fresh.
- ``AWAITING_CLARIFICATION`` — confidence < 0.7, required field missing,
  or ambiguous entity. We send a template-based question (§3.4). The
  owner's reply re-enters A3 with conversation history (§3.2).
- ``AWAITING_CONFIRMATION`` — destructive/high-impact action. We send
  the confirmation prompt. The owner's YES/NO decides what happens next.
  §3.3: re-ask once on ambiguous reply, then cancel.
- ``EXECUTING`` — action in flight (transient).

The confidence threshold is a named constant, easy to tune.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .composer import (
    compose_completion,
    compose_confirmation,
    compose_confirmation_with_diff,
    compose_error,
    compose_undo_done,
    compose_undo_error,
)
from .i18n import translate
from .intent import IntentParser, IntentParseResult
from .reply import ReplySender
from .retry import retry_with_backoff
from .session import ActiveSiteStore, SessionState, SessionStore
from .trackb import TrackBClient, TrackBError

logger = logging.getLogger("track_a.routing")

# Confidence at or above which a parsed intent may proceed without a
# clarifying question. Tune this to trade aggressiveness vs. errors.
CONFIDENCE_THRESHOLD = 0.75

# How many clarification turns we allow before giving up and asking the
# owner to send the full request in one message.
CLARIFICATION_MAX_TURNS = 3

# ---------------------------------------------------------------------------
# Reply texts — resolved at call time via translate() for i18n support.
# ---------------------------------------------------------------------------


def _no_intent_question() -> str:
    return translate("no_intent_question")


def _still_unsure() -> str:
    return translate("still_unsure")


def _confirmation_reask() -> str:
    return translate("confirmation_reask")


def _confirmation_cancelled() -> str:
    return translate("confirmation_cancelled")

# ---------------------------------------------------------------------------
# Required-field knowledge (mirrors shared-contract/intent.schema.json)
#
# NOTE: This module uses the content_types registry from Track B for
# extensibility. For Track A (which doesn't import Track B), we maintain
# a local fallback that mirrors the registry. In production, Track A
# would receive this info from Track B via the contract or API.
# ---------------------------------------------------------------------------

# Fallback for when content_types registry is not available (Track A)
_REQUIRED_ON_CREATE_FALLBACK: dict[str, tuple[str, ...]] = {
    "job": ("title", "description"),
    "announcement": ("title", "body"),
    "business_info": (),
    "image": ("slot",),
    "page": (),  # page_content_update is always an update
}

# Field question translation keys, keyed by "content_type.field".
_FIELD_QUESTION_KEYS: dict[str, str] = {
    "job.title": "field_job_title",
    "job.description": "field_job_description",
    "job.location": "field_job_location",
    "job.remote": "field_job_remote",
    "job.category": "field_job_category",
    "announcement.title": "field_announcement_title",
    "announcement.body": "field_announcement_body",
    "image.slot": "field_image_slot",
    "image.media_url": "field_image_media_url",
    "business_info.phone": "field_business_info_phone",
    "business_info.hours": "field_business_info_hours",
    "business_info.address": "field_business_info_address",
    "business_info.prices": "field_business_info_prices",
    "page.title": "field_page_title",
    "page.content": "field_page_content",
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
        if action != "delete" and not fields.get("media_url") and not fields.get("media_base64"):
            # No separate field name; represent "the image itself" by the
            # media_url slot so the question reads naturally.
            missing.append("media_url")
        return missing

    if action != "create":
        return []  # update/delete accept partial sets

    return [
        name
        for name in _REQUIRED_ON_CREATE_FALLBACK.get(content_type or "", ())
        if not fields.get(name)
    ]


def targeted_question(content_type: str | None, field: str | None) -> str:
    """One targeted clarifying question for the gap, never a generic one."""
    if content_type and field:
        key = _FIELD_QUESTION_KEYS.get(f"{content_type}.{field}")
        if key:
            return translate(key)
        return translate("field_generic", field=field)
    return _no_intent_question()


def _is_yes(text: str) -> bool:
    """§3.3: exact affirmative word set (case-insensitive)."""
    normalized = re.sub(r"[^a-z ]", "", (text or "").strip().lower())
    return normalized in {
        "yes",
        "yeah",
        "yep",
        "confirm",
        "ok",
        "okay",
        "go ahead",
        "do it",
    }


def _is_no(text: str) -> bool:
    """§3.3: exact negative word set (case-insensitive)."""
    normalized = re.sub(r"[^a-z ]", "", (text or "").strip().lower())
    return normalized in {
        "no",
        "nope",
        "cancel",
        "stop",
        "dont",
        "don't",
    }


def _is_undo(text: str) -> bool:
    """§3.5: undo command variants (case-insensitive)."""
    normalized = re.sub(r"[^a-z ]", "", (text or "").strip().lower())
    return normalized in {
        "undo",
        "undo that",
        "undo last change",
        "revert",
    }


def _is_recap(text: str) -> bool:
    """§10 Recap: match recap/history commands (case-insensitive)."""
    normalized = re.sub(r"[^a-z ]", "", (text or "").strip().lower())
    return normalized in {
        "recap",
        "history",
        "recent changes",
        "what have i changed",
        "what have i done",
        "show my changes",
    }


# §3.1: Destructive actions require AWAITING_CONFIRMATION before executing.
# Non-destructive actions skip confirmation and go straight to execution.
_DESTRUCTIVE_ACTIONS = frozenset({"delete"})
_DESTRUCTIVE_CONTENT_TYPES = frozenset({"business_info"})


def _is_destructive(intent: dict[str, Any]) -> bool:
    """§3.1: Does this intent require confirmation before executing?"""
    action = intent.get("action", "")
    content_type = intent.get("content_type", "")
    return action in _DESTRUCTIVE_ACTIONS or (
        action == "update" and content_type in _DESTRUCTIVE_CONTENT_TYPES
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@dataclass
class RouteOutcome:
    branch: str  # state machine outcome: "confirm" | "clarify" | "unclear" | "onboarding" | "undo"
    reply_text: str | None = None
    intent: dict[str, Any] | None = None  # confirmation-ready intent (A5)
    asked_field: str | None = None
    reason: str = ""
    site_id: str | None = None  # active site id (set by onboarding/switch)


def _intent_summary(intent: dict[str, Any]) -> str:
    """Short human summary of a parsed intent, for confirmation questions."""
    action = intent.get("action", "change")
    content_type = intent.get("content_type", "")
    fields = intent.get("fields") or {}
    verb = {"create": "post", "update": "update", "delete": "remove"}.get(action, "change")

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
    return translate("confirm_low_confidence", summary=_intent_summary(intent))


class IntentRouter:
    """Decides the branch for a parse result and drives the loops.

    `handle_message` is the entrypoint the webhook will call.  It checks
    the owner's session (pending clarification / confirmation /
    escalation), re-enters A3 parsing with conversation context when
    mid-clarification, routes the result, and — when the branch produces
    a reply — sends it to the owner via the injected sender.  A5 adds the
    confirmation exchange: a YES submits the pending intent to Track B
    and replies per the result; a NO cancels without any write call.

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
        sender: Any = None,
        trackb: TrackBClient | Any = None,
        onboarding: Any = None,
        active_sites: ActiveSiteStore | None = None,
        reliability: Any = None,
    ) -> None:
        self.parser = parser or IntentParser()
        self.sessions = sessions if sessions is not None else SessionStore()
        self.threshold = threshold
        self.log_escalation = log_escalation or (lambda owner, msg: None)
        self.sender = sender or ReplySender()
        self.trackb = trackb or TrackBClient(base_url="http://127.0.0.1:8200")
        # Persistent per-owner active site tracker (survives session clears).
        self.active_sites = active_sites or ActiveSiteStore()
        # PRD §12: the owner-facing onboarding conversation (or None to
        # disable). Onboarding messages are intercepted BEFORE intent
        # parsing so a URL or application password is never parsed as a
        # content request.
        self.onboarding = onboarding
        # §6: Reliability layer for circuit breaker on Track B calls.
        self.reliability = reliability  # ReliabilityLayer or None (tests)

    async def handle_message(
        self, owner_id: str, message_text: str, *, source: str = "text"
    ) -> RouteOutcome:
        """Route one owner message through the state machine (§3); send any reply.

        ``source`` is "text" or "voice" — set by the caller based on the
        inbound message type.  Voice notes trigger the §4.1 echo-back
        sub-step before entering the normal state machine.
        """
        message_text = (message_text or "").strip()
        state = self.sessions.get(owner_id)

        # --- onboarding (PRD §12): an active walkthrough always wins, and
        # a fresh trigger starts one — but never hijack a message while a
        # confirmation decision is pending.
        # Site-switch commands are also routed through onboarding even when
        # the owner is mid-flow (they interrupt onboarding but not
        # confirmation). ---
        if self.onboarding is not None and (
            self.onboarding.is_active(owner_id)
            or self.onboarding.is_switch_site_trigger(message_text)
            or (state is None and self.onboarding.is_trigger(message_text))
        ):
            outcome = await self.onboarding.handle(owner_id, message_text)
            if outcome is not None:
                # Capture site_id from onboarding/switch into the session
                # and the persistent active-site tracker.
                if outcome.site_id is not None:
                    self.active_sites.set(owner_id, outcome.site_id)
                    if state is None:
                        state = SessionState(site_id=outcome.site_id)
                    else:
                        state.site_id = outcome.site_id
                    self.sessions.set(owner_id, state)
                return await self._send(owner_id, outcome)

        # --- §4.1: Voice echo confirmation ---
        # Voice notes always echo the transcript before acting, regardless
        # of confidence. This sub-step happens BEFORE the state machine.
        if state is not None and state.state == "VOICE_AWAITING_ECHO":
            outcome = await self._handle_voice_echo_reply(owner_id, state, message_text)
            return await self._send(owner_id, outcome)

        if source == "voice" and (state is None or state.state == "IDLE"):
            # §4.1 step 3: Always echo the transcript back before acting.
            outcome = self._handle_voice_echo(owner_id, message_text, state)
            return await self._send(owner_id, outcome)

        # --- UNDO command (promised in the completion message). Only when
        # no confirmation decision is pending. §3.5 ---
        if _is_undo(message_text) and (
            state is None or state.state != "AWAITING_CONFIRMATION"
        ):
            active_site = self.active_sites.get(owner_id)
            outcome = await self._handle_undo(owner_id, site_id=active_site)
            return await self._send(owner_id, outcome)

        # --- §10 RECAP command ---
        if _is_recap(message_text) and (
            state is None or state.state != "AWAITING_CONFIRMATION"
        ):
            outcome = await self._handle_recap(owner_id)
            return await self._send(owner_id, outcome)

        # --- mid-conversation: awaiting a YES/NO (§3.3 AWAITING_CONFIRMATION) ---
        if state is not None and state.state == "AWAITING_CONFIRMATION":
            outcome = await self._handle_confirmation_reply(owner_id, state, message_text)
            return await self._send(owner_id, outcome)

        # --- normal path: parse (with conversation history if clarifying) ---
        context = (
            _format_exchange(state) if state is not None and state.state == "AWAITING_CLARIFICATION" else None
        )
        parse = await self.parser.parse(message_text, owner_id, context=context)
        outcome = self._route(owner_id, parse, message_text, prior=state)
        if outcome.intent is not None:
            if outcome.reason == "confirmation_ready":
                # Destructive action: stage at Track B, then ask for confirmation.
                outcome = await self._stage_pending(owner_id, outcome)
            elif outcome.reason == "non_destructive_ready":
                # §3.1: Non-destructive action: execute immediately, no confirmation.
                outcome = await self._submit_pending(owner_id, outcome.intent)
        return await self._send(owner_id, outcome)

    async def _send(self, owner_id: str, outcome: RouteOutcome) -> RouteOutcome:
        """Send the outcome's reply (if any) and return it unchanged."""
        if outcome.reply_text is not None:
            await self.sender.send(owner_id, outcome.reply_text)
        return outcome

    async def _trackb_call(self, owner_id: str, coro_fn: Any) -> Any:
        """Execute a Track B call with circuit breaker (§6.3).

        When the reliability layer is available, wraps the call with retry
        + backoff + tenant status management.  On final failure, raises
        CircuitBreakerError with the classified error code.

        Falls back to direct call when reliability is not wired (tests).
        """
        if self.reliability is None:
            return await coro_fn()
        # Resolve tenant_id from owner_id for the circuit breaker.
        from .tenant_store import get_tenant_by_sender

        tenant = get_tenant_by_sender(self.reliability.db_path, owner_id)
        if tenant is None:
            # No tenant record — legacy mode, no circuit breaker.
            return await coro_fn()
        breaker = self.reliability.circuit_breaker(tenant["id"])
        return await breaker.call(coro_fn)

    # -- confirmation exchange (A5) ---------------------------------------

    async def _handle_confirmation_reply(
        self, owner_id: str, state: SessionState, message_text: str
    ) -> RouteOutcome:
        """§3.3: AWAITING_CONFIRMATION reply handling with re-ask logic."""
        intent = state.pending_intent
        if intent is None:
            self.sessions.clear(owner_id)
            return RouteOutcome(
                branch="confirm",
                reply_text=translate("confirm_no_pending_intent"),
                reason="no_pending_intent",
            )

        if _is_no(message_text):
            # §3.3: AWAITING_CONFIRMATION → IDLE on negative reply.
            # Tell Track B to discard the staged pending, then clear locally.
            await self._discard_pending(owner_id, intent)
            self.sessions.clear(owner_id)
            return RouteOutcome(
                branch="confirm",
                reply_text=translate("cancel_reply"),
                reason="cancelled",
            )

        if _is_yes(message_text):
            # §3.3: AWAITING_CONFIRMATION → EXECUTING on affirmative.
            return await self._submit_pending(owner_id, intent)

        # §3.3: Not a clear yes/no — re-ask once.
        # "if the second reply is also unmatched, cancel the pending action
        # and return to IDLE, telling the owner it was cancelled."
        if state.re_ask_count >= 1:
            # Second unmatched reply: cancel and return to IDLE.
            await self._discard_pending(owner_id, intent)
            self.sessions.clear(owner_id)
            return RouteOutcome(
                branch="confirm",
                reply_text=_confirmation_cancelled(),
                reason="confirmation_cancelled",
            )

        # First unmatched reply: re-ask once.
        state.re_ask_count += 1
        self.sessions.set(owner_id, state)
        return RouteOutcome(
            branch="confirm",
            reply_text=_confirmation_reask(),
            reason="confirmation_reask",
        )

    async def _stage_pending(self, owner_id: str, outcome: RouteOutcome) -> RouteOutcome:
        """Hold the confirmation-ready intent at Track B (B3) before asking.

        The confirmation prompt only goes out once the intent is staged and
        has a pending change_id at Track B; a failure here becomes an error
        reply instead of a confirmation that can never resolve.
        """
        intent = outcome.intent
        assert intent is not None
        try:
            result = await self._trackb_call(
                owner_id,
                lambda: self.trackb.submit_intent(intent),
            )
        except Exception as exc:  # transport, contract violation, or circuit breaker
            logger.warning("staging failed for owner %s: %s", owner_id, exc)
            self.sessions.clear(owner_id)
            # §6.3: map to owner-facing error message if circuit breaker tripped
            from .reliability import CircuitBreakerError, owner_message_for_error

            if isinstance(exc, CircuitBreakerError):
                reply = exc.owner_message
            else:
                reply = compose_error(None)
            return RouteOutcome(
                branch="confirm",
                reply_text=reply,
                reason="stage_failed",
            )

        status = result.get("status")
        if status == "needs_confirmation":
            # §10 Draft/preview: for high-impact edits, update the confirmation
            # message with a before/after diff from Track B's staging result.
            before = result.get("before")
            after = result.get("after")
            if before and after and _is_destructive(intent):
                new_reply = compose_confirmation_with_diff(intent, before, after)
                outcome = RouteOutcome(
                    branch=outcome.branch,
                    reply_text=new_reply,
                    intent=outcome.intent,
                    reason=outcome.reason,
                )
            return outcome  # staged; send the confirmation prompt

        # Defensive: a Track B that executes immediately on stage (the old
        # stub, or a misconfigured deployment) has already published.
        self.sessions.clear(owner_id)
        if status == "success":
            return RouteOutcome(
                branch="confirm",
                reply_text=compose_completion(result.get("live_url")),
                reason="stage_published",
            )
        if status == "failed":
            return RouteOutcome(
                branch="confirm",
                reply_text=compose_error(result.get("error_message")),
                reason="stage_failed",
            )
        logger.warning("unexpected stage result status %r for owner %s", status, owner_id)
        return RouteOutcome(
            branch="confirm",
            reply_text=compose_error(f"Unexpected response from the publisher ({status!r})."),
            reason="unexpected_status",
        )

    async def _discard_pending(self, owner_id: str, intent: dict[str, Any]) -> None:
        """Relay the NO to Track B so the staged pending is discarded."""
        try:
            await self._trackb_call(
                owner_id,
                lambda: self.trackb.submit_intent(intent, decision="no"),
            )
        except Exception as exc:
            # The owner's intent is cleared locally regardless; Track B's
            # TTL expires the stale pending.
            logger.warning("discard of pending intent failed for owner %s: %s", owner_id, exc)

    async def _handle_undo(self, owner_id: str, *, site_id: str | None = None) -> RouteOutcome:
        """Reply UNDO: reverse the owner's most recent change via Track B."""
        try:
            result = await self._trackb_call(
                owner_id,
                lambda: self.trackb.undo(owner_id, site_id=site_id),
            )
        except Exception as exc:
            logger.warning("undo call failed for owner %s: %s", owner_id, exc)
            from .reliability import CircuitBreakerError

            if isinstance(exc, CircuitBreakerError):
                reply = exc.owner_message
            else:
                reply = compose_undo_error(None)
            return RouteOutcome(
                branch="undo",
                reply_text=reply,
                reason="undo_error",
            )
        if result.get("status") == "success":
            return RouteOutcome(
                branch="undo",
                reply_text=compose_undo_done(result.get("live_url")),
                reason="undo_done",
            )
        return RouteOutcome(
            branch="undo",
            reply_text=compose_undo_error(result.get("error_message")),
            reason="undo_failed",
        )

    async def _handle_recap(self, owner_id: str) -> RouteOutcome:
        """§10 Recap: show the owner their last 5 changes from Track B."""
        try:
            changes = await self.trackb.list_changes(owner_id, limit=5)
        except Exception as exc:
            logger.warning("list_changes failed for owner %s: %s", owner_id, exc)
            return RouteOutcome(
                branch="recap",
                reply_text=translate("recap_empty"),
                reason="recap_error",
            )
        if not changes:
            return RouteOutcome(
                branch="recap",
                reply_text=translate("recap_empty"),
                reason="recap_empty",
            )
        lines = [translate("recap_header")]
        for i, change in enumerate(changes, 1):
            time_ago = _relative_time(change.get("created_at"))
            summary = _change_summary(change)
            if change.get("action") == "undo":
                lines.append(
                    translate("recap_undo_entry", index=i, time_ago=time_ago, summary=summary)
                )
            else:
                lines.append(
                    translate("recap_entry", index=i, time_ago=time_ago, summary=summary)
                )
        return RouteOutcome(
            branch="recap",
            reply_text="\n".join(lines),
            reason="recap_shown",
        )

    async def _submit_pending(self, owner_id: str, intent: dict[str, Any]) -> RouteOutcome:
        """YES: resolve the staged confirmation and reply per the result."""
        try:
            result = await self._trackb_call(
                owner_id,
                lambda: self.trackb.submit_intent(intent, decision="yes"),
            )
        except TrackBError as exc:
            logger.warning("Track B contract violation for owner %s: %s", owner_id, exc)
            return RouteOutcome(
                branch="confirm",
                reply_text=compose_error(None),
                reason="submit_error",
            )
        except Exception as exc:  # transport failure or circuit breaker
            logger.warning("Track B submit failed for owner %s: %s", owner_id, exc)
            from .reliability import CircuitBreakerError

            if isinstance(exc, CircuitBreakerError):
                reply = exc.owner_message
            else:
                reply = compose_error(None)
            return RouteOutcome(
                branch="confirm",
                reply_text=reply,
                reason="submit_error",
            )

        status = result.get("status")
        if status == "success":
            self.sessions.clear(owner_id)
            return RouteOutcome(
                branch="confirm",
                reply_text=compose_completion(result.get("live_url")),
                reason="publish_success",
            )
        if status == "failed":
            # Keep the pending intent: the error invites "Want to try again?",
            # so a follow-up YES retries and a NO cancels.
            return RouteOutcome(
                branch="confirm",
                reply_text=compose_error(result.get("error_message")),
                reason="publish_failed",
            )
        if status == "needs_confirmation":
            # Defensive: shouldn't normally occur post-YES; re-ask.
            return RouteOutcome(
                branch="confirm",
                reply_text=compose_confirmation(intent),
                reason="needs_confirmation",
            )

        # Unknown status: never fake success.
        logger.warning("unexpected Track B result status %r for owner %s", status, owner_id)
        return RouteOutcome(
            branch="confirm",
            reply_text=compose_error(f"Unexpected response from the publisher ({status!r})."),
            reason="unexpected_status",
        )

    # -- branch decisions --------------------------------------------------

    def _route(
        self,
        owner_id: str,
        parse: IntentParseResult,
        original_message: str,
        prior: SessionState | None,
    ) -> RouteOutcome:
        # §3.1: unsupported → unclear → AWAITING_CLARIFICATION with template question.
        if parse.status == "unsupported":
            return self._clarify(
                owner_id,
                prior,
                asked_field=None,
                intent=None,
                original_message=original_message,
                reason="unsupported",
            )

        if parse.status == "low_confidence" or parse.intent is None:
            return self._clarify(
                owner_id,
                prior,
                asked_field=None,
                intent=None,
                original_message=original_message,
                reason="low_confidence_no_intent",
            )

        intent = parse.intent
        # Inject the active site_id from the session so Track B resolves
        # the correct site for multi-site owners.
        if prior is not None and prior.site_id is not None:
            intent["site_id"] = prior.site_id
            # Ensure the active site tracker stays current.
            self.active_sites.set(owner_id, prior.site_id)
        missing = missing_required_fields(intent)
        if parse.confidence >= self.threshold and not missing:
            if _is_destructive(intent):
                # §3.1: IDLE → AWAITING_CONFIRMATION for destructive actions.
                self.sessions.set(
                    owner_id,
                    SessionState(
                        state="AWAITING_CONFIRMATION",
                        pending_intent=intent,
                        site_id=prior.site_id if prior else None,
                    ),
                )
                return RouteOutcome(
                    branch="confirm",
                    intent=intent,
                    reply_text=compose_confirmation(intent),
                    reason="confirmation_ready",
                )
            else:
                # §3.1: IDLE → IDLE (non-destructive): no confirmation needed.
                # The intent is complete and safe — execute immediately.
                # Return the intent for the caller to submit to Track B.
                return RouteOutcome(
                    branch="confirm",
                    intent=intent,
                    reply_text=compose_confirmation(intent),
                    reason="non_destructive_ready",
                )

        if missing:
            return self._clarify(
                owner_id,
                prior,
                asked_field=missing[0],
                intent=intent,
                original_message=original_message,
                reason=f"missing_field:{missing[0]}",
            )
        return self._clarify(
            owner_id,
            prior,
            asked_field=None,
            intent=intent,
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
        """§3.1: IDLE → AWAITING_CLARIFICATION with template-based question (§3.4)."""
        turns = (prior.turns if prior is not None and prior.state == "AWAITING_CLARIFICATION" else 0) + 1
        if turns > CLARIFICATION_MAX_TURNS:
            self.sessions.clear(owner_id)
            return RouteOutcome(
                branch="clarify",
                reply_text=_still_unsure(),
                reason="max_turns",
            )

        # §3.2: context_history — append owner message, build LLM context.
        exchange = _exchange(prior, original_message)
        if asked_field:
            question = targeted_question(
                intent.get("content_type") if intent else None, asked_field
            )
        elif intent is not None:
            question = _confirmation_question(intent)
        else:
            # §3.4: template-based clarification for unsupported/unclear.
            question = _no_intent_question()
        exchange.append({
            "role": "assistant",
            "text": question,
            "at": datetime.now(UTC).isoformat(),
        })

        self.sessions.set(
            owner_id,
            SessionState(
                state="AWAITING_CLARIFICATION",
                pending_intent=intent,
                asked_field=asked_field,
                turns=turns,
                exchange=exchange,
                context_history=exchange,
                site_id=prior.site_id if prior else None,
            ),
        )
        return RouteOutcome(
            branch="clarify",
            reply_text=question,
            asked_field=asked_field,
            reason=reason,
        )

    # -- §4.1: Voice echo confirmation ------------------------------------

    def _handle_voice_echo(
        self, owner_id: str, transcript: str, prior: SessionState | None
    ) -> RouteOutcome:
        """§4.1 step 3: Always echo the transcript back before acting.

        If confidence < 0.5, prepend a caveat. The owner's reply determines
        what happens next: affirmative → use transcript as the instruction;
        any other reply → treat the reply as a corrected instruction.
        """
        # Extract confidence from session or default low.
        # For incoming voice notes, confidence comes from the transcription
        # provider. We store it in the session for the echo step.
        confidence = 0.9  # default for echo; actual confidence set by caller
        if prior is not None and prior.voice_confidence > 0:
            confidence = prior.voice_confidence

        # §4.1 step 4: prepend caveat if confidence < 0.5
        if confidence < 0.5:
            echo_text = translate(
                "voice_echo_low_confidence", transcript=transcript
            )
        else:
            echo_text = translate("voice_echo", transcript=transcript)

        # Set session to VOICE_AWAITING_ECHO, storing the transcript.
        self.sessions.set(
            owner_id,
            SessionState(
                state="VOICE_AWAITING_ECHO",
                voice_transcript=transcript,
                voice_confidence=confidence,
                site_id=prior.site_id if prior else None,
            ),
        )
        return RouteOutcome(
            branch="clarify",
            reply_text=echo_text,
            reason="voice_echo",
        )

    async def _handle_voice_echo_reply(
        self, owner_id: str, state: SessionState, message_text: str
    ) -> RouteOutcome:
        """§4.1 step 5: Owner's reply to the voice echo.

        Affirmative → proceed to normal intent extraction using the
        transcript as raw_input, source="voice".
        Any other reply → treat the reply itself as the corrected
        instruction (text), discard the transcript.
        """
        transcript = state.voice_transcript or ""
        site_id = state.site_id

        if _is_yes(message_text):
            # §4.1 step 5: affirmative → use transcript as the instruction.
            self.sessions.clear(owner_id)
            # Re-enter the normal state machine with the transcript.
            context = None  # fresh parse, no clarification context
            parse = await self.parser.parse(transcript, owner_id, context=context)
            outcome = self._route(owner_id, parse, transcript, prior=None)
            if outcome.intent is not None:
                if outcome.reason == "confirmation_ready":
                    outcome = await self._stage_pending(owner_id, outcome)
                elif outcome.reason == "non_destructive_ready":
                    outcome = await self._submit_pending(owner_id, outcome.intent)
            # Propagate site_id from the voice session.
            if site_id is not None and outcome.intent is not None:
                outcome.intent["site_id"] = site_id
            return outcome

        # §4.1 step 5: any other reply → treat as corrected instruction.
        # Discard the transcript, parse the correction as text.
        self.sessions.clear(owner_id)
        parse = await self.parser.parse(message_text, owner_id, context=None)
        outcome = self._route(owner_id, parse, message_text, prior=None)
        if outcome.intent is not None:
            if outcome.reason == "confirmation_ready":
                outcome = await self._stage_pending(owner_id, outcome)
            elif outcome.reason == "non_destructive_ready":
                outcome = await self._submit_pending(owner_id, outcome.intent)
        if site_id is not None and outcome.intent is not None:
            outcome.intent["site_id"] = site_id
        return outcome

    # NOTE: _handle_escalation_reply removed — §3 replaces escalate with
    # unclear → AWAITING_CLARIFICATION.  The escalation logging is retained
    # for developer handoff if needed in the future, but the routing path
    # no longer has an escalate branch.


# ---------------------------------------------------------------------------
# Exchange / context helpers
# ---------------------------------------------------------------------------


def _relative_time(iso_timestamp: str | None) -> str:
    """Convert an ISO timestamp to a human-readable relative time string."""
    if not iso_timestamp:
        return "recently"
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        diff = (now - ts).total_seconds()
        if diff < 60:
            return "just now"
        if diff < 3600:
            mins = int(diff / 60)
            return f"{mins} minute{'s' if mins != 1 else ''} ago"
        if diff < 86400:
            hours = int(diff / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = int(diff / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    except (ValueError, TypeError):
        return "recently"


def _change_summary(change: dict[str, Any]) -> str:
    """Generate a plain-language summary of a change for the recap command."""
    action = change.get("action", "changed")
    content_type = change.get("content_type", "content")
    after = change.get("after") or {}

    verb = {"create": "Added", "update": "Updated", "delete": "Removed"}.get(
        action, "Changed"
    )

    if content_type == "job":
        title = after.get("title") or "a job posting"
        return f"{verb.lower()} job '{title}'"
    if content_type == "announcement":
        title = after.get("title") or "an announcement"
        return f"{verb.lower()} announcement '{title}'"
    if content_type == "business_info":
        fields = list(after.keys()) if after else ["business info"]
        return f"{verb.lower()} {', '.join(fields)}"
    if content_type == "image":
        slot = after.get("slot", "image")
        return f"{verb.lower()} {slot} image"
    return f"{verb.lower()} {content_type}"


def _exchange(prior: SessionState | None, latest_owner_message: str | None) -> list[dict[str, str]]:
    """§3.2: Return the bounded transcript, optionally appending the new message.

    Each entry has ``role``, ``text``, and ``at`` (ISO timestamp) per spec.
    Keeps only the last 6 turns so the LLM context stays short.
    """
    turns = list(prior.exchange) if prior is not None else []
    if latest_owner_message:
        turns.append({
            "role": "owner",
            "text": latest_owner_message,
            "at": datetime.now(UTC).isoformat(),
        })
    return turns[-6:]


def _format_exchange(state: SessionState) -> str:
    """Render the prior exchange as context for the next A3 parse call."""
    lines = ["Previous exchange with this owner:"]
    for turn in state.exchange:
        speaker = "Owner" if turn.get("role") == "owner" else "Assistant"
        lines.append(f"{speaker}: {turn.get('text', '')}")
    return "\n".join(lines)
