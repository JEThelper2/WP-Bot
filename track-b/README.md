# WP-Bot Track B — WordPress site/state service

Two parts:

1. A **stub app** (`track_b.stub.create_stub_app`) that only survives as
   a legacy test double — the Integration Phase is done, and Track A now
   calls the real API below.
2. The **real API** (`track_b.main.create_app`): `POST /intent` stages or
   resolves intents (B3), applies them through the B2 allowlist + B1
   WordPress client, logs every write (B4), plus `POST /undo` and
   `POST /sites/onboard` (B5). The WordPress client sandbox lives in
   `wp-sandbox/`.

Track A stages a confirmation-ready intent here (getting a pending
`change_id` back) and relays the owner's YES/NO as `?decision=yes|no`;
"UNDO" from the owner hits `POST /undo`. The cross-service end-to-end
suite for all four core flows (publish, undo, clarification loop,
escalation) is `track-b/tests/test_integration_phase.py` — both real
apps over HTTP.

## Pending-confirmation store

`track_b.pending` holds an intent that passed the B2 gate but is awaiting
the owner's YES/NO — the only release valve into the write pipeline:

- `stage_pending(intent)` → stores the full intent keyed by `owner_id`
  with a generated pending `change_id` and a **15-minute TTL** (a pending
  confirmation never sits around indefinitely).
- `resolve_pending(owner_id, decision)` → `yes` returns the staged intent
  for the write pipeline to execute (B1+B2); `no` discards it; nothing
  staged returns a clear `nothing_pending` outcome; a YES after TTL
  expiry returns `expired` ("window passed — resend"), so **a stale write
  is never executed**. A meta key with a 60s grace window over the intent
  TTL is what lets the store tell "expired" apart from "never staged".

`RedisPendingStore` is the production implementation (TTL enforced by
Redis itself); `InMemoryPendingStore` backs tests with an injectable
clock. Set `WPBOT_REDIS_URL` (default `redis://localhost:6379/0`).

## Onboarding validation (PRD §12, step 3)

`POST /sites/onboard` (payload `site_url`, `username`, `app_password`,
`owner_id`) validates a submission against the live site, then persists
an onboarded site record — site_id, owner_id, **encrypted** credentials,
the default B2 allowlist config (`PILOT_SITE_CONFIG`), status `active`
(SQLite via `track_b.onboarding.OnboardedSiteStore`). Validation is a
**read-only probe** (`users/me`) that distinguishes the failure modes
Track A's onboarding conversation must explain:

| Reason | Means |
|---|---|
| `invalid_url` | not a plausible http(s) URL (rejected before any HTTP) |
| `unreachable` | site could not be reached |
| `not_wordpress` | no WordPress REST API at the URL |
| `invalid_credentials` | application password rejected (401/403) |
| `insufficient_permissions` | user lacks `edit_posts` (e.g. Subscriber, not Editor+) |

Success returns `site_id`; failure is HTTP 422 with the specific
`reason`. **Tradeoff:** read-only validation checks the `edit_posts`
capability rather than performing a test write-then-undo — stronger
confidence would require mutating the site during onboarding, which v1
avoids. A failed onboarding persists nothing, and the password never
reaches disk in plaintext or a log.

The **owner-facing conversation** that drives this endpoint lives in
Track A (`track_a.onboarding.OnboardingFlow`): trigger → site URL →
username → application password, then the B5 call; each failure reason
is relayed back in plain language and the owner only re-sends the piece
that was wrong. Conversation e2e (hermetic fake WordPress) is
`track-b/tests/test_onboarding_flow.py`; the real-sandbox variant is
`track-b/tests/test_onboarding_sandbox.py` (same env vars as
`test_integration_wp.py`).

## Change log + undo (PRD §11)

Every successful write logs a row to **Postgres** (`change_log` via
`track_b.changelog`): change_id, owner_id, content_type, action, the
**full `before`/`after` state**, timestamp, and live_url. Logging runs in
the same flow as the write — `apply_intent` treats a write that cannot be
logged as a **failure** (undo depends entirely on this log, so an
unlogged write must never be trusted). `asyncpg` is a lazy, optional
dependency (`pip install -e "./track-b[postgres]"`); `InMemoryChangeLog`
backs the unit tests.

`track_b.undo.undo(owner_id)` finds the owner's most recent change, takes
its `before` state, and **reverse-applies it through the B1 client** —
never a re-guess: create-undo deletes the post, update-undo writes the
prior field values back exactly, delete-undo re-creates from `before`.
The undo is itself logged as a new row (`action="undo"`, `undo_of` the
original), so the trail stays complete and the undo is undoable. A 24h
window (`UNDO_WINDOW_SECONDS`) rejects undos of older changes with a
clear reason; no prior change returns a clear "nothing to undo".

Run the Postgres integration tests against the sandbox:

```bash
docker compose -f track-b/pg-sandbox/docker-compose.yml up -d
WPBOT_PG_TEST_URL=postgres://wpbot:wpbot@localhost:5433/wpbot \
  pytest track-b/tests/test_changelog_pg.py -v
```

## Allowlist gate (B2) — the PRD guardrail in code

No intent reaches the WordPress client without passing, in order:

1. `validate_intent()` against `shared-contract/intent.schema.json` —
   Track A's validation is never trusted;
2. the site's allowlist config (`track_b.allowlist.SiteConfig`, currently
   a hardcoded `PILOT_SITE_CONFIG` for the demo site, structured to become
   per-site config later): the content_type must be **enabled** for the
   site, and every field must be in that content type's **field mapping**;
3. only then does `apply_intent()` dispatch to the WordPress client
   (create/update/delete by title, business_info option, image upload).

Any rejection is a contract-valid result object with
`status: "failed"` and a clear `error_message` — never an exception, and
**never a WordPress write**. The pilot config enables job, announcement,
and business_info; image (v1.5) ships disabled.

## ⚠️ Security guardrail: Editor-level user + application passwords (REQUIRED)

Every WordPress site Track B touches **must** use:

- a **dedicated WordPress user with the `Editor` role** — never
  `Administrator`, never the site owner's own account;
- an **application password** (WP admin → Users → Profile →
  *Application Passwords*; or `wp user application-password create` via
  wp-cli) — never the user's normal login password, never plaintext
  anywhere.

This is a PRD requirement, not a suggestion. An Editor can manage posts,
categories, and media — everything Track B needs — but cannot change
settings, install plugins, or modify users. That bounds the blast radius
if Track B's credentials are ever compromised.

**Consequence for business_info:** WordPress's core REST `settings`
endpoint requires `manage_options` (Administrator), which the guardrail
forbids. Business-info updates therefore go through the bundled
mu-plugin's `wpbot/v1/business-info` route (gated on `edit_posts`, writes
only allowlisted option keys). Install it on any site that needs
business_info updates:

```bash
cp track-b/wp-sandbox/mu-plugins/wpbot-business-info.php \
   <site>/wp-content/mu-plugins/
```

## Credentials: encrypted at rest, never logged

`track_b.secrets` stores per-site credentials in SQLite as **Fernet
ciphertext only** (plaintext never touches disk — a test asserts this),
and every error raised by the client is scrubbed so an application
password can never reach a log line or a result object's `error_message`.

```python
from track_b.secrets import CredentialStore, Vault
from cryptography.fernet import Fernet

store = CredentialStore("trackb.db", Vault(Fernet.generate_key()))
store.set_credentials("https://example.com", "editor", app_password)
```

**Production must set `WPBOT_SECRETS_KEY`** (a base64 Fernet key):

```bash
WPBOT_SECRETS_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

Without it, a per-process dev key is used and stored credentials become
undecryptable after restart.

## WordPress client operations

All calls return a `ChangeRecord(before, after, post_id, live_url)` with
**actual field-level state captured pre- and post-write** — the data a
result object's `before`/`after` need. Nothing returns bare success/fail.

| Operation | What it does |
|---|---|
| `create_post(content_type, fields)` | `job`/`announcement`. Uses the custom post type `jobs` when the site registers one, otherwise a standard post assigned to the `jobs`/`announcements` category (PRD §6). `before` is `None`. |
| `update_post(post_id, fields)` | Partial update; fetches the current state first so `before` is real. |
| `delete_post(post_id)` | Trashes the post (recoverable; leaves the live site immediately). |
| `find_post_by_title(content_type, title)` | Resolves a post id by exact title match — how update/delete intents (which carry no post id in the contract) find their target. |
| `update_site_option(fields)` | business_info singleton via the mu-plugin route (see guardrail above). |
| `upload_and_replace_image(slot, media)` | **v1.5, optional for MVP.** Uploads to the Media Library, never deletes the previous image, and points the slot at the new URL (slot must be in the allowlist in `track_b.config`). |

```python
import asyncio
from track_b.wordpress import WordPressClient


async def main():
    client = WordPressClient(
        "https://example.com",
        "editor",
        app_password,  # from CredentialStore
    )
    created = await client.create_post(
        "job", {"title": "Part-time Barista", "description": "$18/hr, downtown"}
    )
    print(created.after, created.live_url)


asyncio.run(main())
```

Errors surface as `WordPressError` with a clear, actionable message
(auth failure vs unreachable site vs missing mu-plugin route) — never an
unhandled exception, and never the password.

## Tests

```bash
# unit tests (mocked WordPress REST API + credential store)
pytest track-b/tests/test_wordpress.py track-b/tests/test_secrets.py

# integration tests — against a REAL WordPress sandbox (Docker):
docker compose -f track-b/wp-sandbox/docker-compose.yml up -d
docker compose -f track-b/wp-sandbox/docker-compose.yml logs -f setup  # wait for "done"

WPBOT_WP_TEST_URL=http://localhost:8090 \
WPBOT_WP_TEST_USERNAME=editor \
WPBOT_WP_TEST_APP_PASSWORD=$(cat track-b/wp-sandbox/_output/app-password.txt) \
pytest track-b/tests/test_integration_wp.py -v
```

The sandbox (`track-b/wp-sandbox/`) runs WordPress + MariaDB + a wp-cli
setup service that installs WordPress, creates the **Editor** user with an
application password, and installs the mu-plugin. Integration coverage:
successful create, update, delete, business_info, and a failure case
(invalid application password → clear error, credential not leaked).

## Run the real API

```bash
# from the repo root
pip install -e ./shared-contract -e ./track-a -e ./track-b
uvicorn track_b.main:app --port 8200
```

The app boots without external services: without `WPBOT_REDIS_URL`-reachable
Redis or `WPBOT_PG_DSN`, it falls back to in-memory pending/change stores
with a warning (dev only — production must set both).

| Endpoint | Behavior |
|---|---|
| `POST /intent` | Accepts an intent object (validated against intent.schema.json). No `decision` → **stage** as pending (B3, `needs_confirmation` result + change_id). `?decision=yes` → resolve, then B2 allowlist → B1 write → B4 log, returning success with real before/after/live_url. `?decision=no` → discard, nothing written. Always a `validate_result()`-checked result object. |
| `POST /undo` | `{owner_id}` → reverse-apply the owner's most recent change through B4 (create→delete, update→write-back, delete→re-create); logs the undo row. |
| `POST /sites/onboard` | PRD §12: validate site credentials, persist onboarded site (see below). |
| `GET /health` | Liveness check. |

Full lifecycle, directly testable without WhatsApp:

```bash
curl -X POST localhost:8200/intent -H 'Content-Type: application/json' \
  -d '{"contract_version":"1.0.0","owner_id":"...","action":"create","content_type":"job","fields":{"title":"Barista","description":"$18/hr"},"confidence":0.95}'
# -> status needs_confirmation, change_id pc-...
curl -X POST "localhost:8200/intent?decision=yes" -H 'Content-Type: application/json' -d '{...same intent...}'
# -> status success, before/after/live_url (write only happens for an
#    onboarded site, and then is logged + undoable within 24h)
```
