"""Shared harness for the cross-service end-to-end suites.

Builds both REAL apps (Track A webhook+router, Track B real API) over
ASGI transports wired exactly like production, with the in-memory
`FakeWordPress` standing in for the WordPress layer (the real-WordPress
sandbox runs where Docker exists — see track-b/wp-sandbox). Used by
test_integration_phase.py (publish / undo / clarify / escalate) and
test_onboarding_flow.py (PRD §12 onboarding conversation).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from wp_fake import SITE, FakeWordPress

from track_a.config import Settings as ASettings
from track_a.intent import IntentParseResult
from track_a.main import create_app as create_track_a_app
from track_a.onboarding import OnboardingFlow
from track_a.pipeline import MessageProcessor
from track_a.routing import IntentRouter
from track_a.store import init_db as track_a_init_db
from track_a.store import log_escalation_request
from track_a.trackb import TrackBClient
from track_b.allowlist import PILOT_SITE_CONFIG
from track_b.changelog import InMemoryChangeLog
from track_b.config import Settings as BSettings
from track_b.main import TrackBServices
from track_b.main import create_app as create_track_b_app
from track_b.onboarding import OnboardedSiteStore, onboard_site
from track_b.pending import InMemoryPendingStore
from track_b.wordpress import WordPressClient

OWNER = "15551234567"
VERIFY_TOKEN = "integration-verify-token"


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: str, text: str) -> None:
        self.sent.append((to, text))


class NoopMedia:
    async def download_media(self, media_ref: str):
        raise AssertionError("no media downloads expected in text-only flows")


class NoopTranscriber:
    async def transcribe(self, payload):
        raise AssertionError("no transcription expected in text-only flows")


class ScriptedParser:
    def __init__(self, *results: IntentParseResult) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    async def parse(self, message_text: str, owner_id: str, *, context=None):
        self.calls.append({"text": message_text, "context": context})
        return self.results.pop(0)


def parse_intent(intent: dict) -> IntentParseResult:
    return IntentParseResult(status="intent", intent=intent, confidence=intent["confidence"])


@dataclass
class World:
    client: TestClient
    fake_wp: FakeWordPress
    sender: RecordingSender
    db: Path
    services: TrackBServices
    router: IntentRouter


def build_world(
    tmp_path: Path,
    *parse_results: IntentParseResult,
    fake: FakeWordPress | None = None,
    onboarding: bool = True,
    seed_site: bool = True,
    probe_through_fake: bool = True,
) -> World:
    """Both real apps over ASGI transports, wired like production.

    `fake` swaps the WordPress double (wrong credentials, subscriber role,
    unreachable, ...). `onboarding=True` wires the PRD §12 onboarding flow
    into the router, as production does. `seed_site=False` starts Track B
    with an empty site store (used by the onboarding suite, which tests
    the owner going from nothing to active). `probe_through_fake=True`
    routes the B5 onboarding probe through the fake's transport (so the
    onboarding e2e is hermetic); the real-sandbox suite sets it False so
    the probe hits the live WordPress install.
    """
    fake = fake or FakeWordPress(expected_auth=("editor", "app-pass"))

    # ---- Track B: real API, in-memory stores, fake WordPress ----
    sites = OnboardedSiteStore(tmp_path / "sites.db")
    if seed_site:
        sites.add_site(
            owner_id=OWNER,
            site_url=SITE,
            username="editor",
            app_password="app-pass",
            allowlist=PILOT_SITE_CONFIG,
        )

    def make_client(site):
        return WordPressClient(
            site.site_url,
            "editor",
            "app-pass",
            client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )

    services = TrackBServices(
        sites=sites,
        pending=InMemoryPendingStore(),
        changelog=InMemoryChangeLog(),
        make_client=make_client,
    )

    def make_onboarding_runner(fake: FakeWordPress):
        """Route B5's probe through the fake (see probe_through_fake)."""

        async def runner(site_url: str, username: str, app_password: str, owner_id: str):
            probe = WordPressClient(
                site_url,
                username,
                app_password,
                client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
            )
            return await onboard_site(
                site_url,
                username,
                app_password,
                owner_id,
                store=sites,
                client=probe,
            )

        return runner

    track_b_app = create_track_b_app(
        settings=BSettings(db_path=tmp_path / "trackb.db"),
        services=services,
        onboarding_runner=make_onboarding_runner(fake) if probe_through_fake else None,
    )
    track_b_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=track_b_app), base_url="http://track-b"
    )

    # ---- Track A: real webhook + router pointed at real Track B ----
    db = tmp_path / "inbound.db"
    track_a_init_db(db)
    sender = RecordingSender()
    trackb = TrackBClient(base_url="http://track-b", client=track_b_http)
    router = IntentRouter(
        parser=ScriptedParser(*parse_results),
        sender=sender,
        trackb=trackb,
        onboarding=OnboardingFlow(trackb=trackb) if onboarding else None,
        log_escalation=lambda owner, msg: log_escalation_request(db, owner, msg),
    )
    processor = MessageProcessor(
        db_path=db,
        media_client=NoopMedia(),
        transcriber=NoopTranscriber(),
        sender=sender,
    )
    track_a_app = create_track_a_app(
        settings=ASettings(
            verify_token=VERIFY_TOKEN,
            track_b_url="http://track-b",
            db_path=db,
        ),
        processor=processor,
        router=router,
    )
    return World(
        client=TestClient(track_a_app),
        fake_wp=fake,
        sender=sender,
        db=db,
        services=services,
        router=router,
    )


def send(client: TestClient, text: str, wam_id: str, owner: str = OWNER):
    """POST a real Meta-format webhook payload for one text message."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "16505551111",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [{"profile": {"name": "Owner"}, "wa_id": owner}],
                            "messages": [
                                {
                                    "from": owner,
                                    "id": wam_id,
                                    "timestamp": "1700000000",
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }
    return client.post("/webhook", json=payload)


def most_recent(world: World):
    """The owner's most recent change-log row (async store, sync test)."""
    import asyncio

    return asyncio.run(world.services.changelog.most_recent(OWNER))
