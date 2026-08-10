# WP-Bot pilot stack (Docker)

Runs the whole pilot in one stack: **Track A** (WhatsApp conversation
service), **Track B** (WordPress site/state service), **Redis** (pending
confirmations), **Postgres** (the PRD §11 change log) — plus a WordPress
sandbox so the Docker-gated integration suites run against a real
WordPress install instead of being skipped.

Requires **Docker Compose v2.20+** (`service_completed_successfully`
dependency condition). Tested targets: Compose v2.20+ on Linux; Docker
Desktop's bundled Compose works the same.

## Quick start

```bash
# 1. (optional) configure WhatsApp/OpenAI keys
cp deploy/.env.example deploy/.env
#    ...edit deploy/.env...

# 2. build + start the pilot
docker compose -f deploy/docker-compose.yml up -d

# 3. (optional) run the FULL test suite — including the gated
#    WordPress + Postgres integration suites — inside the stack
docker compose -f deploy/docker-compose.yml --profile test run --rm test
```

Shortcuts via the root `Makefile`:

```bash
make up      # build + start the stack
make logs    # follow all service logs
make test    # full suite incl. gated suites
make down    # stop the stack
```

## What you get

| Address | Service |
|---|---|
| http://localhost:8000 | Track A — webhook at `/webhook`, health at `/health` |
| http://localhost:8200 | Track B — `/intent`, `/undo`, `/sites/onboard` |
| http://localhost:8090 | WordPress sandbox (Editor user `editor`, app password in `wpbot_wp_output` volume) |
| (internal) | Redis :6379, Postgres :5432 (user/db `wpbot`/`wpbot`) |

The WordPress sandbox is set up automatically (wp-cli creates the Editor
user + application password; never admin credentials), and Track A/B
healthcheck-wait on their dependencies.

## Running without real keys (degraded but functional)

- **No `OPENAI_API_KEY`** → the intent parser treats every request as
  low-confidence and the bot asks a clarifying question (the designed
  fallback). Set the key for real parsing.
- **No WhatsApp credentials** → outbound replies are logged instead of
  sent, and Media API calls fail. To go live, expose Track A with a
  tunnel (see `track-a/README.md`) and set the webhook in the Meta
  developer portal.
- **No `WPBOT_SECRETS_KEY`** → Track B encrypts credentials with a
  per-process dev key (undecryptable after restart). Generate one with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## The test service

`docker compose --profile test run --rm test` builds a test image
containing the whole repo and runs `pytest -q` inside the stack network,
so the previously-skipped suites run for real:

- `track-b/tests/test_integration_wp.py` — WordPress client lifecycle
  against the sandbox (`WPBOT_WP_TEST_URL=http://wordpress`, app password
  read from the `wpbot_wp_output` volume);
- `track-b/tests/test_onboarding_sandbox.py` — the PRD §12 onboarding
  conversation against the sandbox;
- `track-b/tests/test_changelog_pg.py` — the Postgres change log
  (`WPBOT_PG_TEST_URL` points at the stack's Postgres).

## Notes

- The per-track `wp-sandbox/` and `pg-sandbox/` compose files still work
  for host-side development; this stack is the self-contained equivalent
  for running everything at once.
- The WordPress sandbox binds `8090:80`; stop this stack before running
  `track-b/wp-sandbox` if you need both (they would fight over the port).
- Build with voice transcription enabled: `docker compose -f
  deploy/docker-compose.yml build --build-arg WITH_TRANSCRIBE=1 track-a`.
