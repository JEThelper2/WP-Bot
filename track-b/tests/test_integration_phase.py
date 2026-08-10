"""Integration Phase: Track A (webhook + router) <-> the real Track B API.

The two services talk over HTTP exactly as they do in production: real
Meta-format webhook payloads into Track A, real intent/undo calls out to
Track B's `/intent` and `/undo` endpoints (ASGI in-process transports in
these tests, real sockets in deployment — same code paths).

Four core flows are exercised end to end:

1. **publish**      — message -> intent -> staged confirmation -> YES ->
                      real WordPress write -> completion with live_url;
2. **undo**         — reply UNDO -> reverse-apply on WordPress -> clear
                      confirmation to the owner;
3. **clarify**      — incomplete request -> one targeted question -> reply
                      re-enters parsing with context -> confirmation;
4. **escalate**     — out-of-scope request -> escalation message -> YES ->
                      logged and retrievable via /escalations.

The WordPress layer is the same in-memory `FakeWordPress` the B1/B2 suites
use (the real-WordPress sandbox runs where Docker exists — see
track-b/wp-sandbox). The parser is scripted (A3's LLM is a pluggable
seam); everything downstream of it is the real code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from shared_contract import CONTRACT_VERSION

from track_a.config import Settings as ASettings
from track_a.intent import IntentParseResult
from track_a.main import create_app as create_track_a_app
from track_a.pipeline import MessageProcessor
from track_a.routing import (
    ESCALATION_CONFIRM_REPLY,
    ESCALATION_REPLY_TEXT,
    IntentRouter,
)
from track_a.store import init_db as track_a_init_db, log_escalation_request
from track_a.trackb import TrackBClient

from track_b.allowlist import PILOT_SITE_CONFIG
from track_b.changelog import InMemoryChangeLog
from track_b.config import Settings as BSettings
from track_b.main import TrackBServices, create_app as create_track_b_app
from track_b.onboarding import OnboardedSiteStore
from track_b.pending import InMemoryPendingStore
from track_b.wordpress import WordPressClient

from wp_fake import SITE, FakeWordPress

OWNER = "15551234567"
VERIFY_TOKEN = "integration-verify-token"

JOB_INTENT = {
    "contract_version": CONTRACT_VERSION,
    "owner_id": OWNER,
    "action": "create",
    "content_type": "job",
    "fields": {"title": "Part-time Barista", "description": "$18/hr downtown"},
    "confidence": 0.95,
}


# ------------------------------------------------------------------ doubles


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
    return IntentParseResult(
        status="intent", intent=intent, confidence=intent["confidence"]
    )


@dataclass
class World:
    client: TestClient
    fake_wp: FakeWordPress
    sender: RecordingSender
    db: Path
    services: TrackBServices
    router: IntentRouter


def build_world(tmp_path: Path, *parse_results: IntentParseResult) -> World:
    """Both real apps over ASGI transports, wired exactly like production."""
    # ---- Track B: real API, in-memory stores, fake WordPress ----
    fake = FakeWordPress(expected_auth=("editor", "app-pass"))
    sites = OnboardedSiteStore(tmp_path / "sites.db")
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
    track_b_app = create_track_b_app(
        settings=BSettings(db_path=tmp_path / "trackb.db"),
        services=services,
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


# ------------------------------------------------------------- publish flow


def test_publish_flow_end_to_end(tmp_path):
    from track_a.composer import compose_completion, compose_confirmation

    world = build_world(tmp_path, parse_intent(dict(JOB_INTENT)))

    resp = send(world.client, "post a job for a barista downtown", "wamid.publish.1")
    assert resp.status_code == 200
    assert resp.json()["received"] == 1
    # Real confirmation composed and sent to the owner.
    assert world.sender.sent[-1] == (OWNER, compose_confirmation(JOB_INTENT))

    # YES -> real Track B resolve -> real write on WordPress.
    send(world.client, "yes", "wamid.publish.2")
    assert world.fake_wp.posts  # the post exists on the site
    post = list(world.fake_wp.posts.values())[0]
    assert post["title"]["raw"] == "Part-time Barista"
    assert post["status"] == "publish"

    # Completion message with the working live_url.
    assert world.sender.sent[-1][0] == OWNER
    assert world.sender.sent[-1][1] == compose_completion(post["link"])

    # The write is on the audit trail (PRD §11), with the staged change_id.
    assert len(world.services.changelog) == 1
    row = await_most_recent(world)
    assert row.content_type == "job"
    assert row.action == "create"
    assert row.before is None
    assert row.after["title"] == "Part-time Barista"


def await_most_recent(world: World):
    import asyncio

    return asyncio.run(world.services.changelog.most_recent(OWNER))


# --------------------------------------------------------------- undo flow


def test_undo_flow_end_to_end(tmp_path):
    from track_a.composer import compose_confirmation

    world = build_world(tmp_path, parse_intent(dict(JOB_INTENT)))

    send(world.client, "post a job for a barista downtown", "wamid.undo.1")
    assert world.sender.sent[-1][1] == compose_confirmation(JOB_INTENT)
    send(world.client, "yes", "wamid.undo.2")
    assert len(world.fake_wp.posts) == 1
    post_id = next(iter(world.fake_wp.posts))
    assert world.fake_wp.posts[post_id]["status"] == "publish"

    # Reply UNDO: the post is trashed on the site and the owner is told.
    send(world.client, "undo", "wamid.undo.3")
    assert world.fake_wp.posts[post_id]["status"] == "trash"
    assert world.sender.sent[-1][0] == OWNER
    assert "reverted" in world.sender.sent[-1][1].lower()

    # The undo itself is logged (undo is undoable; trail complete).
    assert len(world.services.changelog) == 2
    undo_row = await_most_recent(world)
    assert undo_row.action == "undo"
    assert undo_row.undo_of is not None


def test_undo_with_nothing_to_undo_gets_clear_reply(tmp_path):
    world = build_world(tmp_path)

    send(world.client, "undo", "wamid.noundo.1")
    assert world.sender.sent[-1][0] == OWNER
    # Track B's "nothing to undo" reason surfaces in plain language.
    assert "no changes found to undo" in world.sender.sent[-1][1].lower()


# ------------------------------------------------------- clarification flow


def test_clarification_loop_end_to_end(tmp_path):
    from track_a.composer import compose_confirmation

    incomplete = dict(JOB_INTENT)
    incomplete["fields"] = {"description": "cash handling"}
    resolved = dict(JOB_INTENT)
    resolved["fields"] = {"title": "Cashier", "description": "cash handling"}

    world = build_world(
        tmp_path,
        parse_intent(incomplete),
        parse_intent(resolved),
    )

    # Incomplete request -> ONE targeted clarifying question.
    send(world.client, "post a job, it involves cash handling", "wamid.clar.1")
    assert world.sender.sent[-1] == (OWNER, "What's the job title?")

    # Reply with the missing info -> re-enters parsing WITH context -> a
    # confirmation for the completed intent.
    send(world.client, "Cashier", "wamid.clar.2")
    assert world.sender.sent[-1] == (OWNER, compose_confirmation(resolved))

    # The re-entry carried the prior exchange as LLM context.
    ctx = world.router.parser.calls[1]["context"]
    assert ctx is not None
    assert "What's the job title?" in ctx

    # YES publishes the clarified request.
    send(world.client, "yes", "wamid.clar.3")
    assert len(world.fake_wp.posts) == 1
    assert list(world.fake_wp.posts.values())[0]["title"]["raw"] == "Cashier"


# ----------------------------------------------------------- escalation flow


def test_escalation_flow_end_to_end(tmp_path):
    world = build_world(tmp_path, IntentParseResult(status="unsupported", confidence=0.0))

    # Out-of-scope request -> the fixed escalation message.
    send(world.client, "redesign my homepage", "wamid.esc.1")
    assert world.sender.sent[-1] == (OWNER, ESCALATION_REPLY_TEXT)

    # YES -> escalation logged, owner confirmed.
    send(world.client, "yes", "wamid.esc.2")
    assert world.sender.sent[-1] == (OWNER, ESCALATION_CONFIRM_REPLY)

    # Logged and retrievable (PRD §10) for a human to pick up.
    resp = world.client.get("/escalations")
    assert resp.json()["count"] == 1
    row = resp.json()["escalations"][0]
    assert row["owner_phone"] == OWNER
    assert row["original_message"] == "redesign my homepage"
    assert row["status"] == "new"
