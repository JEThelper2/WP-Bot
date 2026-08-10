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

**Not built yet (next milestones):** routing parsed intents to Track B,
real replies, clarifying-question flow (A4).

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `WHATSAPP_VERIFY_TOKEN` | `wp-bot-dev-verify-token` | Token Meta must send in the verification handshake. |
| `WHATSAPP_API_TOKEN` | *(empty)* | System-user access token for the WhatsApp Media API (voice-note download). |
| `WHATSAPP_GRAPH_API_VERSION` | `v21.0` | Graph API version for media + (later) send calls. |
| `OPENAI_API_KEY` | *(empty)* | Key for the LLM intent parser (`OpenAILLMClient`). |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model used by the intent parser. |
| `TRACK_B_URL` | `http://127.0.0.1:8200` | Where Track B (stub) lives. |
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

# terminal 1: stub Track B
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
