"""Owner-facing onboarding conversation (PRD §12), wired to B5.

A business owner goes from \"nothing set up\" to \"system active\" purely by
messaging WhatsApp. The flow is a small guided walkthrough (static
instructional text is the v1 deliverable; a video is post-feedback per
PRD §17):

    trigger (\"set up my website\")
      -> Step 1: site URL
      -> Step 2: WordPress username (Editor role)
      -> Step 3: application password
      -> POST /sites/onboard (B5 validation endpoint)
      -> success:   \"You're all set\" + example phrasings for the three
                    supported content types
      -> failure:   the SPECIFIC reason in plain language (invalid URL /
                    unreachable / not WordPress / invalid credentials /
                    insufficient permissions), plus the clear next step —
                    never a generic \"something went wrong\".

On failure the owner stays in the flow and only has to re-send the piece
that was wrong (the bad URL, or just the password, etc.), so a typo
doesn't restart the whole walkthrough. \"cancel\"/\"stop\" aborts cleanly.

The flow owns its own per-owner state (separate from the intent router's
session store — onboarding never mixes with intent parsing). `handle`
returns a `RouteOutcome` when the message belongs to onboarding and
`None` otherwise, so `IntentRouter` can delegate without knowing the
details.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .routing import RouteOutcome

logger = logging.getLogger("track_a.onboarding")

# ---------------------------------------------------------------------------
# Static walkthrough + outcome texts (PRD §12 step-by-step instructions)
# ---------------------------------------------------------------------------

ONBOARD_STEP_URL = (
    "Let's connect your website so you can update it by WhatsApp. "
    "Step 1 — send me your site URL, for example https://mybusiness.com"
)

ONBOARD_STEP_USERNAME = (
    "Got it — {url}. "
    "Step 2 — send the WordPress username of the user I'll use (it needs "
    "the Editor role). Don't have one? In WordPress: Users → Add New → "
    "role: Editor."
)

ONBOARD_STEP_APP_PASSWORD = (
    "Step 3 — send the application password for that user. To create one: "
    "WordPress → Users → Profile → Application Passwords → name it "
    "'wp-bot' → Add New, then copy the password (it's only shown once)."
)

ONBOARD_SUCCESS = (
    "You're all set — {url} is connected and ready. "
    "Try asking me: "
    "\"Post a job: part-time barista, $18/hr\", "
    "\"Add an announcement: closed July 4th\", or "
    "\"Change my hours to 9-6\". "
    "I'll always confirm with you before anything goes live."
)

ONBOARD_INVALID_URL = (
    "That doesn't look like a website address. Send your full URL, "
    "for example https://mybusiness.com"
)

ONBOARD_UNREACHABLE = (
    "I couldn't reach {url}. Check that the site is online and the "
    "address is correct, then send it again."
)

ONBOARD_NOT_WORDPRESS = (
    "That site is online, but I couldn't find WordPress on it. Only "
    "WordPress sites can be connected — double-check the address and "
    "send it again."
)

ONBOARD_INVALID_CREDENTIALS = (
    "WordPress rejected the application password. It's created at "
    "WordPress → Users → Profile → Application Passwords (copy it right "
    "away — it's only shown once). Send the application password again."
)

ONBOARD_INSUFFICIENT_PERMISSIONS = (
    "The login works, but that WordPress user can't edit posts. The user "
    "needs the Editor role (WordPress → Users, edit the user). Send the "
    "username and application password of an Editor-level user."
)

ONBOARD_CANCELLED = (
    "No problem — setup cancelled. Message me 'set up my website' anytime "
    "to connect your site."
)

# ---------------------------------------------------------------------------
# Trigger / cancel detection
# ---------------------------------------------------------------------------

_TRIGGER_EXACT = {
    "onboard", "onboarding", "setup", "set up", "get started", "get me started",
    "sign me up", "sign up", "connect website", "connect my website",
    "connect my site", "connect my wordpress", "connect wordpress",
    "add my website", "add my site", "add my wordpress", "add wordpress",
    "start setup", "begin setup", "connect my blog",
}

_TRIGGER_PREFIXES = (
    "set up my", "connect my", "add my site", "add my website",
    "add my wordpress", "get my website",
)

_CANCEL_WORDS = {
    "cancel", "stop", "quit", "abort", "never mind", "forget it", "skip it",
}


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", (text or "").strip().lower())).strip()


def is_onboard_trigger(message_text: str) -> bool:
    normalized = _normalize(message_text)
    if not normalized:
        return False
    if normalized in _TRIGGER_EXACT:
        return True
    return any(normalized.startswith(p) for p in _TRIGGER_PREFIXES)


def _is_cancel(message_text: str) -> bool:
    return _normalize(message_text) in _CANCEL_WORDS


def _plausible_url(raw: str) -> str | None:
    """A quick sanity check mirroring B5's normalization.

    Returns the normalized URL, or None if the message clearly isn't a
    website address (caught locally so we never bother Track B with
    garbage). B5 still does its own authoritative validation.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.split("@")[-1].split(":")[0]
    if not host or ("." not in host and host != "localhost"):
        return None
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@dataclass
class OnboardState:
    stage: str  # "url" | "username" | "app_password"
    site_url: str | None = None
    username: str | None = None


class OnboardingFlow:
    """Guided onboarding conversation; delegates the B5 call to Track B."""

    def __init__(self, trackb: Any) -> None:
        self.trackb = trackb
        self._sessions: dict[str, OnboardState] = {}
        self._lock = threading.Lock()

    # -- session helpers ---------------------------------------------------

    def is_active(self, owner_id: str) -> bool:
        with self._lock:
            return owner_id in self._sessions

    def is_trigger(self, message_text: str) -> bool:
        return is_onboard_trigger(message_text)

    def _get(self, owner_id: str) -> OnboardState | None:
        with self._lock:
            return self._sessions.get(owner_id)

    def _set(self, owner_id: str, state: OnboardState) -> None:
        with self._lock:
            self._sessions[owner_id] = state

    def _clear(self, owner_id: str) -> None:
        with self._lock:
            self._sessions.pop(owner_id, None)

    # -- conversation ------------------------------------------------------

    async def handle(self, owner_id: str, message_text: str) -> RouteOutcome | None:
        """Route one message through the onboarding flow.

        Returns a RouteOutcome when the message belongs to onboarding
        (mid-flow, or a fresh trigger), else None.
        """
        text = (message_text or "").strip()
        state = self._get(owner_id)

        if state is None:
            if not is_onboard_trigger(text):
                return None
            self._set(owner_id, OnboardState(stage="url"))
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_STEP_URL,
                reason="onboard_start",
            )

        # Mid-flow: cancel beats everything else.
        if _is_cancel(text):
            self._clear(owner_id)
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_CANCELLED,
                reason="onboard_cancelled",
            )

        if state.stage == "url":
            url = _plausible_url(text)
            if url is None:
                return RouteOutcome(
                    branch="onboarding",
                    reply_text=ONBOARD_INVALID_URL,
                    reason="invalid_url",
                )
            state.site_url = url
            state.stage = "username"
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_STEP_USERNAME.format(url=url),
                reason="onboard_step_username",
            )

        if state.stage == "username":
            state.username = text
            state.stage = "app_password"
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_STEP_APP_PASSWORD,
                reason="onboard_step_app_password",
            )

        # stage == "app_password": submit to B5.
        return await self._submit(owner_id, state, text)

    async def _submit(
        self, owner_id: str, state: OnboardState, app_password: str
    ) -> RouteOutcome:
        try:
            result = await self.trackb.onboard_site(
                site_url=state.site_url or "",
                username=state.username or "",
                app_password=app_password,
                owner_id=owner_id,
            )
        except Exception as exc:
            # Transport failure: same plain-language path as unreachable.
            logger.warning(
                "onboarding call failed for owner %s: %s", owner_id, exc
            )
            state.stage = "url"
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_UNREACHABLE.format(url=state.site_url or ""),
                reason="unreachable",
            )

        if result.get("status") == "success":
            self._clear(owner_id)
            logger.info("owner %s onboarded site %s", owner_id, state.site_url)
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_SUCCESS.format(url=state.site_url or ""),
                reason="onboarded",
            )

        # Failed: map the B5 reason to the right re-entry point.
        reason = result.get("reason")
        if reason == "invalid_url":
            state.site_url = None
            state.stage = "url"
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_INVALID_URL,
                reason="invalid_url",
            )
        if reason == "unreachable":
            state.stage = "url"
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_UNREACHABLE.format(url=state.site_url or ""),
                reason="unreachable",
            )
        if reason == "not_wordpress":
            state.stage = "url"
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_NOT_WORDPRESS,
                reason="not_wordpress",
            )
        if reason == "invalid_credentials":
            # Keep URL + username; only the password was wrong.
            state.stage = "app_password"
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_INVALID_CREDENTIALS,
                reason="invalid_credentials",
            )
        if reason == "insufficient_permissions":
            # The user lacks edit rights; they need an Editor-level user.
            state.username = None
            state.stage = "username"
            return RouteOutcome(
                branch="onboarding",
                reply_text=ONBOARD_INSUFFICIENT_PERMISSIONS,
                reason="insufficient_permissions",
            )

        # Unknown reason: be honest that nothing was set up.
        logger.warning(
            "unexpected onboarding failure reason %r for owner %s",
            reason,
            owner_id,
        )
        state.stage = "url"
        return RouteOutcome(
            branch="onboarding",
            reply_text=ONBOARD_UNREACHABLE.format(url=state.site_url or ""),
            reason="onboard_failed",
        )
