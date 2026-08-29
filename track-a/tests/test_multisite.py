"""Multi-site flow: onboard two sites, list them, switch between them,
and verify intents carry the correct site_id.

Covers:
- ActiveSiteStore lifecycle (get/set/clear)
- OnboardingFlow site-switch commands (list sites, switch by id/url)
- site_id propagation through routing into intent objects
- Undo passes site_id to Track B
- Two owners don't interfere with each other's active site
"""

import asyncio
from typing import Any

from shared_contract import CONTRACT_VERSION
from track_a.i18n import translate
from track_a.onboarding import (
    OnboardingFlow,
    is_switch_site_trigger,
    parse_switch_site,
)
from track_a.routing import IntentRouter
from track_a.session import ActiveSiteStore, SessionState, SessionStore
from track_a.intent import IntentParseResult

OWNER_A = "15551111111"
OWNER_B = "15552222222"

SITE_A = {"site_id": "site-alpha", "site_url": "https://alpha.com", "status": "active"}
SITE_B = {"site_id": "site-beta", "site_url": "https://beta.com", "status": "active"}


# ---------------------------------------------------------------------------
# ActiveSiteStore
# ---------------------------------------------------------------------------


class TestActiveSiteStore:
    def test_get_returns_none_for_unknown_owner(self):
        store = ActiveSiteStore()
        assert store.get("nobody") is None

    def test_set_and_get(self):
        store = ActiveSiteStore()
        store.set("owner-1", "site-42")
        assert store.get("owner-1") == "site-42"

    def test_clear_removes_entry(self):
        store = ActiveSiteStore()
        store.set("owner-1", "site-42")
        store.clear("owner-1")
        assert store.get("owner-1") is None

    def test_clear_unknown_owner_is_noop(self):
        store = ActiveSiteStore()
        store.clear("nobody")  # should not raise

    def test_set_overwrites_previous(self):
        store = ActiveSiteStore()
        store.set("owner-1", "site-1")
        store.set("owner-1", "site-2")
        assert store.get("owner-1") == "site-2"

    def test_owners_are_independent(self):
        store = ActiveSiteStore()
        store.set("owner-a", "site-1")
        store.set("owner-b", "site-2")
        assert store.get("owner-a") == "site-1"
        assert store.get("owner-b") == "site-2"
        store.clear("owner-a")
        assert store.get("owner-b") == "site-2"


# ---------------------------------------------------------------------------
# OnboardingFlow site-switch commands
# ---------------------------------------------------------------------------


class FakeTrackBForSwitch:
    """Minimal Track B stub for testing multi-site commands."""

    def __init__(self, sites: list[dict] | None = None, *, active_site: str | None = None):
        self._sites = sites or []
        self.active_site_set: list[tuple[str, str]] = []  # (site_id, owner_id)

    async def list_sites(self, owner_id: str) -> list[dict]:
        return self._sites

    async def set_active_site(self, site_id: str, owner_id: str) -> None:
        self.active_site_set.append((site_id, owner_id))

    async def onboard_site(self, **kwargs: Any) -> dict:
        raise AssertionError("onboard_site should not be called in switch tests")


class TestSwitchSiteTriggers:
    def test_list_sites_variants(self):
        for msg in ["list sites", "my sites", "show sites", "which site", "site list"]:
            assert is_switch_site_trigger(msg), f"'{msg}' should be a list trigger"

    def test_switch_to_variants(self):
        for msg in ["switch to alpha", "use site beta", "change site to gamma", "select site delta"]:
            assert is_switch_site_trigger(msg), f"'{msg}' should be a switch trigger"

    def test_non_switch_messages(self):
        for msg in ["hello", "set up my website", "post a job", "undo"]:
            assert not is_switch_site_trigger(msg), f"'{msg}' should not be a switch trigger"

    def test_parse_switch_list_returns_none(self):
        assert parse_switch_site("list sites") is None
        assert parse_switch_site("my sites") is None

    def test_parse_switch_extracts_target(self):
        assert parse_switch_site("switch to alpha") == "alpha"
        assert parse_switch_site("use site beta.com") == "beta.com"

    def test_parse_switch_case_insensitive(self):
        assert parse_switch_site("Switch To Alpha") == "alpha"


class TestOnboardingFlowSiteSwitch:
    def _make_flow(self, sites: list[dict] | None = None) -> OnboardingFlow:
        return OnboardingFlow(trackb=FakeTrackBForSwitch(sites))

    async def _handle(self, flow: OnboardingFlow, owner: str, msg: str):
        return await flow.handle(owner, msg)

    def test_list_sites_empty(self):
        flow = self._make_flow([])
        outcome = asyncio.run(self._handle(flow, OWNER_A, "list sites"))
        assert outcome is not None
        assert outcome.reason == "no_sites"
        assert "don't have any sites" in outcome.reply_text

    def test_list_sites_shows_all(self):
        flow = self._make_flow([SITE_A, SITE_B])
        outcome = asyncio.run(self._handle(flow, OWNER_A, "my sites"))
        assert outcome is not None
        assert outcome.reason == "sites_listed"
        assert "alpha.com" in outcome.reply_text
        assert "beta.com" in outcome.reply_text
        assert "site-alpha" in outcome.reply_text

    def test_switch_to_site_by_id(self):
        flow = self._make_flow([SITE_A, SITE_B])
        outcome = asyncio.run(self._handle(flow, OWNER_A, "switch to site-beta"))
        assert outcome is not None
        assert outcome.reason == "site_switched"
        assert outcome.site_id == "site-beta"
        assert "beta.com" in outcome.reply_text

    def test_switch_to_site_by_url(self):
        flow = self._make_flow([SITE_A, SITE_B])
        outcome = asyncio.run(self._handle(flow, OWNER_A, "switch to alpha.com"))
        assert outcome is not None
        assert outcome.reason == "site_switched"
        assert outcome.site_id == "site-alpha"

    def test_switch_to_unknown_site(self):
        flow = self._make_flow([SITE_A])
        outcome = asyncio.run(self._handle(flow, OWNER_A, "switch to nonexistent"))
        assert outcome is not None
        assert outcome.reason == "site_not_found"
        assert "alpha.com" in outcome.reply_text

    def test_switch_persists_active_site(self):
        tb = FakeTrackBForSwitch([SITE_A, SITE_B])
        flow = OnboardingFlow(trackb=tb)
        asyncio.run(self._handle(flow, OWNER_A, "switch to site-beta"))
        assert tb.active_site_set == [("site-beta", OWNER_A)]

    def test_list_sites_error(self):
        class FailTrackB:
            async def list_sites(self, owner_id):
                raise ConnectionError("timeout")
            async def set_active_site(self, site_id, owner_id):
                pass

        flow = OnboardingFlow(trackb=FailTrackB())
        outcome = asyncio.run(self._handle(flow, OWNER_A, "list sites"))
        assert outcome is not None
        assert outcome.reason == "site_list_error"


# ---------------------------------------------------------------------------
# site_id propagation through IntentRouter
# ---------------------------------------------------------------------------


class ScriptedParser:
    def __init__(self, *results: IntentParseResult) -> None:
        self.results = list(results)
        self.calls: list[dict[str, str | None]] = []

    async def parse(self, message_text: str, owner_id: str, *, context=None):
        self.calls.append({"text": message_text, "context": context})
        return self.results.pop(0)


class FakeTrackBForRouter:
    def __init__(self):
        self.staged_intents: list[dict] = []
        self.undo_calls: list[tuple[str, str | None]] = []

    async def submit_intent(self, intent: dict, *, decision: str | None = None) -> dict:
        if decision is None:
            self.staged_intents.append(intent)
            return {
                "contract_version": CONTRACT_VERSION,
                "status": "needs_confirmation",
                "change_id": "pc-test",
                "before": None,
                "after": None,
                "live_url": None,
                "error_message": None,
            }
        if decision == "yes":
            return {
                "contract_version": CONTRACT_VERSION,
                "status": "success",
                "change_id": "pc-test",
                "before": None,
                "after": None,
                "live_url": "https://example.com/live",
                "error_message": None,
            }
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "change_id": "pc-test",
            "before": None,
            "after": None,
            "live_url": None,
            "error_message": None,
        }

    async def undo(self, owner_id: str, *, site_id: str | None = None) -> dict:
        self.undo_calls.append((owner_id, site_id))
        return {
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "change_id": "ch-undo",
            "before": None,
            "after": None,
            "live_url": "https://example.com/undone",
            "error_message": None,
        }


def _make_intent(content_type: str = "job", fields: dict | None = None, action: str = "delete") -> dict:
    """Default to destructive action so confirmation flow is exercised."""
    return {
        "contract_version": CONTRACT_VERSION,
        "owner_id": OWNER_A,
        "action": action,
        "content_type": content_type,
        "fields": fields or {"title": "Barista", "description": "$18/hr"},
        "confidence": 0.9,
    }


def _parse(intent: dict) -> IntentParseResult:
    return IntentParseResult(status="intent", intent=intent, confidence=intent["confidence"])


def _build_router(parser: ScriptedParser, trackb: FakeTrackBForRouter | None = None) -> IntentRouter:
    return IntentRouter(
        parser=parser,
        sessions=SessionStore(),
        trackb=trackb or FakeTrackBForRouter(),
    )


class TestSiteIdInRouting:
    def test_site_id_captured_from_onboarding_switch(self):
        """After switching sites, the session carries site_id."""
        tb = FakeTrackBForRouter()
        active_sites = ActiveSiteStore()
        onb = OnboardingFlow(trackb=FakeTrackBForSwitch([SITE_A, SITE_B]))
        router = _build_router(ScriptedParser(), trackb=tb)
        router.onboarding = onb
        router.active_sites = active_sites

        # Switch to site-beta via onboarding
        outcome = asyncio.run(router.handle_message(OWNER_A, "switch to site-beta"))
        assert outcome.site_id == "site-beta"
        assert active_sites.get(OWNER_A) == "site-beta"

    def test_site_id_injected_into_intent(self):
        """When session has site_id, the staged intent carries it."""
        intent = _make_intent()
        tb = FakeTrackBForRouter()
        active_sites = ActiveSiteStore()
        active_sites.set(OWNER_A, "site-beta")
        onb = OnboardingFlow(trackb=FakeTrackBForSwitch([SITE_A, SITE_B]))
        router = _build_router(ScriptedParser(_parse(intent)), trackb=tb)
        router.onboarding = onb
        router.active_sites = active_sites

        # Set up session with site_id
        router.sessions.set(OWNER_A, SessionState(site_id="site-beta"))

        outcome = asyncio.run(router.handle_message(OWNER_A, "post a job"))
        assert outcome.branch == "confirm"
        # The staged intent should carry site_id
        assert tb.staged_intents[0].get("site_id") == "site-beta"

    def test_site_id_persists_across_clarification(self):
        """site_id survives through the clarify state."""
        # Use low confidence to trigger clarification
        intent_low = _make_intent(fields={"description": "cash handling"}, action="delete")
        intent_low["confidence"] = 0.4  # below threshold → clarify
        intent_full = _make_intent()
        tb = FakeTrackBForRouter()
        active_sites = ActiveSiteStore()
        active_sites.set(OWNER_A, "site-alpha")
        onb = OnboardingFlow(trackb=FakeTrackBForSwitch([SITE_A]))
        router = _build_router(ScriptedParser(_parse(intent_low), _parse(intent_full)), trackb=tb)
        router.onboarding = onb
        router.active_sites = active_sites
        router.sessions.set(OWNER_A, SessionState(site_id="site-alpha"))

        # First message: clarify (low confidence)
        outcome1 = asyncio.run(router.handle_message(OWNER_A, "remove a job"))
        assert outcome1.branch == "clarify"
        state = router.sessions.get(OWNER_A)
        assert state.site_id == "site-alpha"

        # Second message: confirm (destructive delete)
        outcome2 = asyncio.run(router.handle_message(OWNER_A, "the cash handling one"))
        assert outcome2.branch == "confirm"
        assert tb.staged_intents[0].get("site_id") == "site-alpha"

    def test_undo_uses_active_site(self):
        """UNDO command passes the active site_id to Track B."""
        tb = FakeTrackBForRouter()
        active_sites = ActiveSiteStore()
        active_sites.set(OWNER_A, "site-beta")
        onb = OnboardingFlow(trackb=FakeTrackBForSwitch([SITE_A, SITE_B]))
        router = _build_router(ScriptedParser(), trackb=tb)
        router.onboarding = onb
        router.active_sites = active_sites

        outcome = asyncio.run(router.handle_message(OWNER_A, "undo"))
        assert outcome.branch == "undo"
        assert outcome.reason == "undo_done"
        assert tb.undo_calls == [(OWNER_A, "site-beta")]

    def test_undo_without_active_site_passes_none(self):
        """UNDO with no active site still works (falls back to owner-level)."""
        tb = FakeTrackBForRouter()
        active_sites = ActiveSiteStore()
        onb = OnboardingFlow(trackb=FakeTrackBForSwitch())
        router = _build_router(ScriptedParser(), trackb=tb)
        router.onboarding = onb
        router.active_sites = active_sites

        outcome = asyncio.run(router.handle_message(OWNER_A, "undo"))
        assert outcome.branch == "undo"
        assert tb.undo_calls == [(OWNER_A, None)]

    def test_two_owners_independent_sites(self):
        """Each owner's active site is independent."""
        tb = FakeTrackBForRouter()
        active_sites = ActiveSiteStore()
        onb = OnboardingFlow(trackb=FakeTrackBForSwitch([SITE_A, SITE_B]))
        router = _build_router(ScriptedParser(), trackb=tb)
        router.onboarding = onb
        router.active_sites = active_sites

        # Owner A switches to site-alpha
        outcome_a = asyncio.run(router.handle_message(OWNER_A, "switch to site-alpha"))
        assert outcome_a.site_id == "site-alpha"

        # Owner B switches to site-beta
        outcome_b = asyncio.run(router.handle_message(OWNER_B, "switch to site-beta"))
        assert outcome_b.site_id == "site-beta"

        assert active_sites.get(OWNER_A) == "site-alpha"
        assert active_sites.get(OWNER_B) == "site-beta"
