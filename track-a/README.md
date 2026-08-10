# WP-Bot Track A — WhatsApp conversation service

Inbound **WhatsApp Cloud API (Meta, direct)** webhook receiver. Current
milestone scope:

- `GET /webhook` — Meta's subscription verification handshake.
- `POST /webhook` — receives inbound WhatsApp messages (text and voice-note
  media), extracts owner phone, message type, raw content / media reference,
  and timestamp, and persists each message to a lightweight SQLite log.
- **Voice notes** — audio messages are downloaded via Meta's Media API and
  transcribed with Whisper (`faster-whisper`). Both text and successfully
  transcribed voice messages are normalized into a single `message_text`
  field, so intent parsing (next milestone) doesn't care which channel the
  owner used.
- **Low-confidence fallback** — if a transcript is empty/garbled, below the
  confidence threshold, or Whisper detects no speech, the message gets no
  `message_text` (intent parsing skips it) and a reply asks the owner to
  resend as text or try the voice note again. The send mechanism is stubbed
  in `track_a.reply` (real Graph API send lands in A5).
- A Track B client (`track_a.trackb.TrackBClient`) that speaks the *real*
  contract shapes against the Track B stub: intents are validated outbound
  with `validate_intent`, results are validated inbound with
  `validate_result`.

- **Intent parsing** — `track_a.intent.IntentParser` turns the normalized
  `message_text` (plus owner id) into an intent object matching
  `shared-contract/intent.schema.json`, validated with `validate_intent`
  before anything else sees it. The LLM returns its own confidence score
  (0.0-1.0) and is prompted to be conservative. Out-of-scope requests
  ("redesign my homepage") produce the `UNSUPPORTED_SENTINEL`
  (`content_type: null`, `confidence: 0`); malformed or contract-rejected
  LLM output is treated as low confidence. `image` is implemented but is
  v1.5 scope, not required for the v1 MVP.

- **Routing (A4)** — `track_a.routing.IntentRouter` routes every parsed
  intent to exactly one branch:
  - `confirm` — confidence ≥ `CONFIDENCE_THRESHOLD` (0.75, a named,
    tunable constant) and all fields required for that content_type/action
    are present (mirrors the contract: job/announcement need title+
    description/body on `create`; business_info is always partial; image
    needs slot + media unless delete).
  - `clarify` — below threshold or missing fields. One *targeted* question
    ("What's the job title?", not "can you clarify"). The owner's reply
    re-enters parsing with the prior exchange as LLM context
    (`track_a.session.SessionStore`, in-memory per owner; swap for Redis in
    a multi-worker deployment). Loop caps at `CLARIFICATION_MAX_TURNS` (3).
  - `escalate` — the unsupported sentinel triggers the fixed escalation
    message; a "yes" logs an escalation request (owner, original message)
    to the `escalation_requests` table (PRD §10), reviewable via
    `GET /escalations`. No matching logic — a human picks these up.

- **Outbound messaging (A5)** — `track_a.composer` turns a validated,
  ready intent into a plain-language confirmation that always ends with an
  explicit YES/NO instruction; `track_a.reply.WhatsAppReplySender` sends it
  via the Graph API (`POST /{version}/{phone_number_id}/messages`,
  Bearer token; falls back to logging when unconfigured). The router's
  confirmation exchange handles the reply:
  - **YES** → resolve the staged confirmation at Track B
    (`decision=yes`) and reply per the result object: `success` →
    completion with the result's `live_url` ("You can undo this within 24h
    by replying UNDO"); `failed` → plain-language error using
    `error_message` (or a generic text) — never a silent failure or a
    false "done"; `needs_confirmation` → re-send the confirmation prompt
    defensively.
  - **NO** → "Okay, cancelled — nothing was changed." — the discard is
    relayed to Track B (`decision=no`, which never touches WordPress) and
    the pending intent is cleared locally.
  - After a failed publish the pending intent is kept so a follow-up YES
    retries and a NO cancels.

- **Integration Phase (real Track B)** — Track A now talks to the *real*
  Track B API over HTTP (two separate services, per PRD §17). The webhook
  drives the whole conversation: message → pipeline → `message_text` →
  `IntentRouter.handle_message`. A confirmation-ready intent is **staged**
  at Track B first (B3 pending store, 15-minute TTL; the confirmation only
  goes out once it has a pending change_id), and the owner's YES/NO is
  relayed as `decision=yes|no` on `/intent`. Reply **UNDO** (promised in
  the completion message) calls Track B's `/undo`, which reverse-applies
  the stored before/after state (B4) and replies with a clear result.
  Both directions still validate against the shared contract at the
  boundary. End-to-end tests for all four flows (publish, undo,
  clarification loop, escalation) live in `track-b/tests/test_integration_phase.py`.

- **Onboarding (PRD §12)** — the owner can go from nothing to "system
  active" by messaging: `track_a.onboarding.OnboardingFlow` intercepts
  onboarding messages (trigger words like "set up my website", or an
  in-progress walkthrough) before intent parsing ever sees them. Guided
  steps: site URL → WordPress username (Editor role) → application
  password, then the B5 `/sites/onboard` endpoint is called. Success
  replies with example phrasings for the three content types; each
  failure reason (invalid URL / unreachable / not WordPress / invalid
  credentials / insufficient permissions) gets its own plain-language
  message and the owner only re-sends the piece that was wrong. The
  walkthrough is static instructional text (v1; a video is post-feedback
  per PRD §17). Conversation e2e lives in
  `track-b/tests/test_onboarding_flow.py`, and the real-sandbox variant
  in `track-b/tests/test_onboarding_sandbox.py`.

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `WHATSAPP_VERIFY_TOKEN` | `wp-bot-dev-verify-token` | Token Meta must send in the verification handshake. |
| `WHATSAPP_API_TOKEN` | *(empty)* | System-user access token for the WhatsApp Media + Messages API. |
| `WHATSAPP_PHONE_NUMBER_ID` | *(empty)* | Business phone number id used for outbound messages. |
| `WHATSAPP_GRAPH_API_VERSION` | `v21.0` | Graph API version for media + send calls. |
| `OPENAI_API_KEY` | *(empty)* | Key for the LLM intent parser (`OpenAILLMClient`). |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model used by the intent parser. |
| `TRACK_B_URL` | `http://127.0.0.1:8200` | Where the real Track B API lives. |
| `WP_BOT_TRACK_A_DB` | `track-a/data/inbound.db` | SQLite file for the inbound message log. |

## Run

```bash
# from the repo root (installs shared-contract, track-a, track-b)
pip install -e ./shared-contract -e ./track-a -e ./track-b

# voice transcription needs the optional Whisper extra
# (pinned to a combo verified on Windows/CPU: faster-whisper 1.0.3 +
# ctranslate2 4.6.0 + setuptools<81. ctranslate2 4.7.x's Windows wheel is
# broken and 4.8.x segfaults at model load on some hosts. VAD is off by
# default because onnxruntime fails to load on some Windows hosts; Whisper's
# own no_speech_prob still drives the speech-detected flag.)
pip install -e "./track-a[transcribe]"

# LLM intent parsing needs the optional openai extra
pip install -e "./track-a[llm]"

# terminal 1: Track B (WordPress site/state service — real API)
uvicorn track_b.main:app --port 8200

# terminal 2: Track A webhook receiver
uvicorn track_a.main:app --port 8000
```

Meta only delivers webhooks to a public HTTPS URL, so for local development
expose port 8000 with a tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8000   # or: ngrok http 8000
```

Then in the Meta Developer portal set the webhook callback URL to
`https://<tunnel>/webhook` with the verify token from `WHATSAPP_VERIFY_TOKEN`.

## Endpoints

| Endpoint | Behavior |
|---|---|
| `GET /health` | Liveness check. |
| `GET /webhook` | Meta verification handshake (echoes `hub.challenge`). |
| `POST /webhook` | Receives a Meta delivery; logs each new message; always answers 200 (404 for unrecognized objects). |
| `GET /messages` | Dev/debug: recent logged messages, newest first. |

## Message pipeline

Every new inbound message is logged and then run through
`track_a.pipeline.MessageProcessor`:

| message_type | result | `message_text` |
|---|---|---|
| `text` | status `text` | the raw body |
| `audio` (voice note) | status `transcribed` | Whisper transcript (when confidence ≥ 0.5 and speech detected) |
| `audio` (bad) | status `low_confidence` / `failed` | `None` + fallback reply asking to resend |
| image/video/etc. | status `unsupported` | `None` |

Voice routing is tested with three real generated clips (440 Hz tone,
white noise, silence) — see `track-a/tests/test_voice.py`.

## Intent parsing

`track_a.intent.IntentParser.parse(message_text, owner_id)` returns an
`IntentParseResult`:

| status | meaning | intent |
|---|---|---|
| `intent` | parsed and contract-validated | the validated intent object |
| `low_confidence` | LLM failed validation / malformed / no answer | `None` |
| `unsupported` | out-of-scope request | `None` (semantic output = `UNSUPPORTED_SENTINEL`) |

The parser only needs `message_text` — it never cares whether the owner
texted or spoke. 15+ sample messages are covered in
`track-a/tests/test_intent.py`.

## Tests

```bash
pytest track-a/tests track-b/tests
```
