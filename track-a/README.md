# WP-Bot Track A — WhatsApp conversation service

Inbound **WhatsApp Cloud API (Meta, direct)** webhook receiver. Current
milestone scope:

- `GET /webhook` — Meta's subscription verification handshake.
- `POST /webhook` — receives inbound WhatsApp messages (text and voice-note
  media), extracts owner phone, message type, raw content / media reference,
  and timestamp, and persists each message to a lightweight SQLite log.
- A Track B client (`track_a.trackb.TrackBClient`) that speaks the *real*
  contract shapes against the Track B stub: intents are validated outbound
  with `validate_intent`, results are validated inbound with
  `validate_result`.

**Not built yet (next milestones):** voice transcription, LLM parsing of
message content into intent objects, sending intents to Track B, replies.

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `WHATSAPP_VERIFY_TOKEN` | `wp-bot-dev-verify-token` | Token Meta must send in the verification handshake. |
| `TRACK_B_URL` | `http://127.0.0.1:8200` | Where Track B (stub) lives. |
| `WP_BOT_TRACK_A_DB` | `track-a/data/inbound.db` | SQLite file for the inbound message log. |

## Run

```bash
# from the repo root (installs shared-contract, track-a, track-b)
pip install -e ./shared-contract -e ./track-a -e ./track-b

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

## Tests

```bash
pytest track-a/tests track-b/tests
```
