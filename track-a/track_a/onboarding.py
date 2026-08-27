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

from shared_contract import normalize_url

from .i18n import translate
from .routing import RouteOutcome

logger = logging.getLogger("track_a.onboarding")

# ---------------------------------------------------------------------------
# Onboarding texts — resolved at call time via translate() for i18n.
# ---------------------------------------------------------------------------


def _onboard_step_url() -> str:
    return translate("onboard_step_url")


def _onboard_step_username(url: str) -> str:
    return translate("onboard_step_username", url=url)


def _onboard_step_app_password() -> str:
    return translate("onboard_step_app_password")


def _onboard_success(url: str) -> str:
    return translate("onboard_success", url=url)


def _onboard_invalid_url() -> str:
    return translate("onboard_invalid_url")


def _onboard_unreachable(url: str) -> str:
    return translate("onboard_unreachable", url=url)


def _onboard_not_wordpress() -> str:
    return translate("onboard_not_wordpress")


def _onboard_invalid_credentials() -> str:
    return translate("onboard_invalid_credentials")


def _onboard_insufficient_permissions() -> str:
    return translate("onboard_insufficient_permissions")


def _onboard_cancelled() -> str:
    return translate("onboard_cancelled")

# ---------------------------------------------------------------------------
# Trigger / cancel detection
# ---------------------------------------------------------------------------

_TRIGGER_EXACT = {
    "onboard",
    "onboarding",
    "setup",
    "set up",
    "get started",
    "get me started",
    "sign me up",
    "sign up",
    "connect website",
    "connect my website",
    "connect my site",
    "connect my wordpress",
    "connect wordpress",
    "add my website",
    "add my site",
    "add my wordpress",
    "add wordpress",
    "start setup",
    "begin setup",
    "connect my blog",
}

_TRIGGER_PREFIXES = (
    "set up my",
    "connect my",
    "add my site",
    "add my website",
    "add my wordpress",
    "get my website",
)

_CANCEL_WORDS = {
    "cancel",
    "stop",
    "quit",
    "abort",
    "never mind",
    "forget it",
    "skip it",
}

# Multi-site triggers: allow the owner to list or switch between sites.
_SWITCH_PREFIXES = (
    "switch to ",
    "use site ",
    "change site to ",
    "change to ",
    "select site ",
    "activate site ",
)

_LIST_TRIGGERS = {
    "list sites",
    "my sites",
    "show sites",
    "which site",
    "what site",
    "site list",
}


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", "", (text or "").strip().lower())).strip()


def _normalize_switch(text: str) -> str:
    """Lowercase, collapse whitespace — but keep hyphens, dots, and slashes
    so site IDs and URLs survive normalization.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.\-/ ]", "", (text or "").strip().lower())).strip()


def is_onboard_trigger(message_text: str) -> bool:
    normalized = _normalize(message_text)
    if not normalized:
        return False
    if normalized in _TRIGGER_EXACT:
        return True
    return any(normalized.startswith(p) for p in _TRIGGER_PREFIXES)


def is_switch_site_trigger(message_text: str) -> bool:
    """Detect 'switch to <site>' or 'list sites' type commands."""
    normalized = _normalize_switch(message_text)
    if not normalized:
        return False
    if normalized in _LIST_TRIGGERS:
        return True
    return any(normalized.startswith(p) for p in _SWITCH_PREFIXES)


def parse_switch_site(message_text: str) -> str | None:
    """Extract the target site hint from a switch command, or None for list."""
    normalized = _normalize_switch(message_text)
    if normalized in _LIST_TRIGGERS:
        return None  # list all sites
    for prefix in _SWITCH_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix):].strip()
    return None


def _is_cancel(message_text: str) -> bool:
    return _normalize(message_text) in _CANCEL_WORDS


def _plausible_url(raw: str) -> str | None:
    """A quick sanity check mirroring B5's normalization.

    Uses the shared ``normalize_url`` from shared-contract.  Returns
    the normalized URL, or None if the message clearly isn't a website
    address.
    """
    return normalize_url(raw)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


@dataclass
class OnboardState:
    stage: str  # "url" | "username" | "app_password"
    site_url: str | None = None
    username: str | None = None


class OnboardingFlow:
    """Guided onboarding conversation; delegates the B5 call to Track B.

    Also handles multi-site switching: 'list sites' shows all onboarded
    sites for the owner, and 'switch to <site>' sets the active site.
    """

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

    def is_switch_site_trigger(self, message_text: str) -> bool:
        return is_switch_site_trigger(message_text)

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
        (mid-flow, fresh trigger, or multi-site commands), else None.
        """
        text = (message_text or "").strip()
        state = self._get(owner_id)

        # --- multi-site commands (independent of onboarding state) ---
        if is_switch_site_trigger(text):
            return await self._handle_site_switch(owner_id, text)

        if state is None:
            if not is_onboard_trigger(text):
                return None
            self._set(owner_id, OnboardState(stage="url"))
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_step_url(),
                reason="onboard_start",
            )

        # Mid-flow: cancel beats everything else.
        if _is_cancel(text):
            self._clear(owner_id)
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_cancelled(),
                reason="onboard_cancelled",
            )

        if state.stage == "url":
            url = _plausible_url(text)
            if url is None:
                return RouteOutcome(
                    branch="onboarding",
                    reply_text=_onboard_invalid_url(),
                    reason="invalid_url",
                )
            state.site_url = url
            state.stage = "username"
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_step_username(url),
                reason="onboard_step_username",
            )

        if state.stage == "username":
            state.username = text
            state.stage = "app_password"
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_step_app_password(),
                reason="onboard_step_app_password",
            )

        # stage == "app_password": submit to B5.
        return await self._submit(owner_id, state, text)

    async def _submit(self, owner_id: str, state: OnboardState, app_password: str) -> RouteOutcome:
        try:
            result = await self.trackb.onboard_site(
                site_url=state.site_url or "",
                username=state.username or "",
                app_password=app_password,
                owner_id=owner_id,
            )
        except Exception as exc:
            # Transport failure: same plain-language path as unreachable.
            logger.warning("onboarding call failed for owner %s: %s", owner_id, exc)
            state.stage = "url"
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_unreachable(state.site_url or ""),
                reason="unreachable",
            )

        if result.get("status") == "success":
            self._clear(owner_id)
            new_site_id = result.get("site_id")
            logger.info("owner %s onboarded site %s (site_id=%s)", owner_id, state.site_url, new_site_id)
            # Mark this as the owner's active site in Track B.
            if new_site_id:
                try:
                    await self.trackb.set_active_site(new_site_id, owner_id)
                except Exception as exc:
                    logger.warning("set_active_site failed after onboard: %s", exc)
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_success(state.site_url or ""),
                reason="onboarded",
                site_id=new_site_id,
            )

        # Failed: map the B5 reason to the right re-entry point.
        reason = result.get("reason")
        if reason == "invalid_url":
            state.site_url = None
            state.stage = "url"
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_invalid_url(),
                reason="invalid_url",
            )
        if reason == "unreachable":
            state.stage = "url"
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_unreachable(state.site_url or ""),
                reason="unreachable",
            )
        if reason == "not_wordpress":
            state.stage = "url"
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_not_wordpress(),
                reason="not_wordpress",
            )
        if reason == "invalid_credentials":
            # Keep URL + username; only the password was wrong.
            state.stage = "app_password"
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_invalid_credentials(),
                reason="invalid_credentials",
            )
        if reason == "insufficient_permissions":
            # The user lacks edit rights; they need an Editor-level user.
            state.username = None
            state.stage = "username"
            return RouteOutcome(
                branch="onboarding",
                reply_text=_onboard_insufficient_permissions(),
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
            branch="onboarding",                reply_text=_onboard_unreachable(state.site_url or ""),
                reason="onboard_failed",
        )

    # -- multi-site commands -----------------------------------------------

    async def _handle_site_switch(self, owner_id: str, message_text: str) -> RouteOutcome:
        """Handle 'list sites' or 'switch to <site>' commands."""
        target = parse_switch_site(message_text)

        try:
            sites = await self.trackb.list_sites(owner_id)
        except Exception as exc:
            logger.warning("list_sites failed for owner %s: %s", owner_id, exc)
            return RouteOutcome(
                branch="onboarding",
                reply_text=translate("site_list_error"),
                reason="site_list_error",
            )

        if not sites:
            return RouteOutcome(
                branch="onboarding",
                reply_text=translate("site_list_empty"),
                reason="no_sites",
            )

        if target is None:
            # List all sites.
            lines = [translate("site_list_header")]
            for s in sites:
                status_mark = "✓" if s.get("status") == "active" else "⚠"
                lines.append(f"  {status_mark} {s['site_url']} (id: {s['site_id']})")
            lines.append("")
            lines.append(translate("site_list_hint"))
            return RouteOutcome(
                branch="onboarding",
                reply_text="\n".join(lines),
                reason="sites_listed",
            )

        # Switch to a specific site: match by site_id or URL substring.
        matched = None
        for s in sites:
            if target in s["site_id"] or target in s["site_url"]:
                matched = s
                break

        if matched is None:
            site_list = ", ".join(s["site_url"].split("//")[-1] for s in sites)
            return RouteOutcome(
                branch="onboarding",
                reply_text=translate("site_not_found", target=target, sites=site_list),
                reason="site_not_found",
            )

        logger.info("owner %s switched to site %s", owner_id, matched["site_id"])
        # Persist the active site in Track B so the dashboard can show it.
        try:
            await self.trackb.set_active_site(matched["site_id"], owner_id)
        except Exception as exc:
            logger.warning("set_active_site failed for %s: %s", matched["site_id"], exc)
        return RouteOutcome(
            branch="onboarding",
            reply_text=translate("site_switched", url=matched["site_url"]),
            reason="site_switched",
            site_id=matched["site_id"],
        )
