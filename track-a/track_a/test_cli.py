#!/usr/bin/env python3
"""Terminal chatbot for manual testing without WhatsApp or Telegram.

Runs the full Track A pipeline (intent parsing → routing → Track B)
in an interactive terminal loop.  Each message you type is treated as
an inbound message from a virtual owner, and the bot's reply is printed
to stdout.

Usage::

    # Default (uses env vars for AI provider, Track B URL, etc.)
    python -m track_a.test_cli

    # Override owner ID (default: "cli_test_owner")
    OWNER_ID=owner_123 python -m track_a.test_cli

    # Dry-run mode (skip Track B calls)
    python -m track_a.test_cli --dry-run

    # Show the parsed intent before routing
    python -m track_a.test_cli --verbose

Environment variables::

    OWNER_ID              — virtual owner ID (default: "cli_test_owner")
    TRACK_B_URL           — Track B base URL (default: http://127.0.0.1:8200)
    AI_PROVIDER           — LLM provider (default: "groq")
    <PROVIDER>_API_KEY    — API key for the selected provider
    DRY_RUN=1             — skip Track B calls (intent parsing still runs)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

# Ensure the track_a package is importable when run from the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class CLISender:
    """Prints replies to the terminal instead of sending them."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    async def send(self, to: str, text: str) -> None:
        print(f"\n🤖 Bot: {text}\n")


class DryRunTrackB:
    """Stub Track B client that returns canned results (no real API calls)."""

    async def submit_intent(self, intent: dict[str, Any], *, decision: str | None = None) -> dict[str, Any]:
        if decision == "yes":
            return {
                "status": "success",
                "change_id": "dry_run_001",
                "live_url": "https://example.com/wp-admin",
            }
        if decision == "no":
            return {"status": "success", "change_id": "dry_run_001"}
        return {
            "status": "needs_confirmation",
            "change_id": "dry_run_001",
            "intent_summary": f"{intent.get('action', 'change')} {intent.get('content_type', 'unknown')}",
        }

    async def undo(self, owner_id: str, *, site_id: str | None = None) -> dict[str, Any]:
        return {"status": "success", "change_id": "dry_run_001", "live_url": "https://example.com"}

    async def onboard_site(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "success",
            "reason": "onboarded",
            "message": "Site onboarded successfully",
            "site_id": "dry_site_001",
            "site_url": kwargs.get("site_url", "https://example.com"),
        }

    async def set_active_site(self, site_id: str, owner_id: str) -> bool:
        return True

    async def list_sites(self, owner_id: str) -> list[dict[str, str]]:
        return [
            {"site_id": "dry_site_001", "site_url": "https://example.com", "status": "active"},
        ]


def build_router(dry_run: bool = False, verbose: bool = False) -> Any:
    """Build a fully-wired IntentRouter for CLI testing."""
    from track_a.ai_provider import get_provider
    from track_a.config import Settings
    from track_a.intent import IntentParser
    from track_a.onboarding import OnboardingFlow
    from track_a.routing import IntentRouter
    from track_a.trackb import TrackBClient

    settings = Settings.from_env()
    sender = CLISender(verbose=verbose)

    # AI provider
    provider_kwargs: dict[str, str] = {}
    if settings.ai_api_key:
        provider_kwargs["api_key"] = settings.ai_api_key
    if settings.ai_model:
        provider_kwargs["model"] = settings.ai_model
    llm = get_provider(settings.ai_provider, **provider_kwargs)
    parser = IntentParser(llm=llm)

    # Track B
    if dry_run:
        trackb = DryRunTrackB()  # type: ignore[assignment]
    else:
        trackb = TrackBClient(base_url=settings.track_b_url)

    onboarding = OnboardingFlow(trackb=trackb)

    return IntentRouter(
        parser=parser,
        sender=sender,
        trackb=trackb,
        onboarding=onboarding,
    )


async def run_cli(owner_id: str, dry_run: bool, verbose: bool) -> None:
    """Main CLI loop."""
    router = build_router(dry_run=dry_run, verbose=verbose)

    print("=" * 60)
    print("  Sitepaw Terminal Chat (manual testing mode)")
    print(f"  Owner ID: {owner_id}")
    print(f"  Track B:  {'dry-run stub' if dry_run else os.environ.get('TRACK_B_URL', 'http://127.0.0.1:8200')}")
    print(f"  AI:       {os.environ.get('AI_PROVIDER', 'groq')}")
    print("=" * 60)
    print()
    print("Type messages as if you were a business owner messaging the bot.")
    print("Commands:  /quit  /reset  /status")
    print()

    while True:
        try:
            user_input = input(f"[{owner_id}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        # Meta-commands
        if user_input == "/quit":
            print("Goodbye!")
            break
        if user_input == "/reset":
            router.sessions.clear(owner_id)
            router.active_sites.clear(owner_id)
            print("[session reset]\n")
            continue
        if user_input == "/status":
            state = router.sessions.get(owner_id)
            active_site = router.active_sites.get(owner_id)
            print(f"  Session: {state.branch if state else 'none'}")
            print(f"  Active site: {active_site or 'none'}")
            if state and state.pending_intent:
                print(f"  Pending intent: {state.pending_intent.get('action')} {state.pending_intent.get('content_type')}")
            print()
            continue

        # Run through the pipeline
        try:
            outcome = await router.handle_message(owner_id, user_input)
            if verbose and outcome:
                print(f"  [branch={outcome.branch} reason={outcome.reason}]")
        except Exception as exc:
            print(f"\n❌ Error: {exc}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal chatbot for Sitepaw testing")
    parser.add_argument("--dry-run", action="store_true", help="Skip Track B API calls (use stub)")
    parser.add_argument("--verbose", action="store_true", help="Show branch/reason metadata")
    parser.add_argument("--owner", default=None, help="Override owner ID (default: OWNER_ID env or cli_test_owner)")
    args = parser.parse_args()

    owner_id = args.owner or os.environ.get("OWNER_ID", "cli_test_owner")
    asyncio.run(run_cli(owner_id, dry_run=args.dry_run, verbose=args.verbose))


if __name__ == "__main__":
    main()
