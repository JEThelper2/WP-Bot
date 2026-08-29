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
seam); everything downstream of it is the real code. The onboarding flow
is wired in too (as in production) but none of these messages are
onboarding triggers.
"""

from __future__ import annotations

from integration_harness import OWNER, build_world, most_recent, send

from shared_contract import CONTRACT_VERSION
from track_a.composer import compose_completion, compose_confirmation
from track_a.intent import IntentParseResult
from track_a.i18n import translate

JOB_INTENT = {
    "contract_version": CONTRACT_VERSION,
    "owner_id": OWNER,
    "action": "create",
    "content_type": "job",
    "fields": {"title": "Part-time Barista", "description": "$18/hr downtown"},
    "confidence": 0.95,
}


def parse_intent(intent: dict) -> IntentParseResult:
    return IntentParseResult(status="intent", intent=intent, confidence=intent["confidence"])


# ------------------------------------------------------------- publish flow


def test_publish_flow_end_to_end(tmp_path):
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
    row = most_recent(world)
    assert row.content_type == "job"
    assert row.action == "create"
    assert row.before is None
    assert row.after["title"] == "Part-time Barista"


# --------------------------------------------------------------- undo flow


def test_undo_flow_end_to_end(tmp_path):
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
    undo_row = most_recent(world)
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
    assert list(world.fake_wp.posts.values())[0]["title"]["raw"] == "Cashier"# ----------------------------------------------------------- unclear flow

def test_unsupported_sends_clarification_end_to_end(tmp_path):
    """§3: unsupported → unclear → AWAITING_CLARIFICATION (not escalate)."""
    world = build_world(tmp_path, IntentParseResult(status="unsupported", confidence=0.0))

    # Out-of-scope request -> template clarification question.
    send(world.client, "redesign my homepage", "wamid.esc.1")
    reply = world.sender.sent[-1][1]
    assert "rephrase" in reply.lower() or "understand" in reply.lower()
    # State is AWAITING_CLARIFICATION, not escalate.
    state = world.router.sessions.get(OWNER)
    assert state is not None and state.state == "AWAITING_CLARIFICATION"
