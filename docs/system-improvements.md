# WP-Bot System Improvements

## Overview

This document describes the 20 improvements made to the WP-Bot system, organized into 5 phases.

## Phase 1: Hardening & Reliability

### 1.1 Session TTL (`track-a/track_a/session.py`)
- Sessions now expire after 15 minutes of inactivity (matching Track B's pending store TTL)
- Stale sessions are lazily evicted on `get()` and eagerly swept by `cleanup()`
- Prevents memory leaks from abandoned clarification/escalation flows

### 1.2 SQLite WAL Mode (`track-a/track_a/store.py`, `track-b/track_b/onboarding.py`, `track-b/track_b/secrets.py`)
- Enabled WAL (Write-Ahead Logging) mode on all SQLite connections
- Improves concurrent read/write performance under FastAPI's async workers
- Backward-compatible with existing databases

### 1.3 Shared httpx.AsyncClient (`track-a/track_a/main.py`, `track-a/track_a/reply.py`)
- Single `httpx.AsyncClient` created at app startup, shared by all HTTP clients
- Lifecycle managed by FastAPI's `lifespan` context manager
- Properly closed on shutdown to prevent resource leaks

### 1.4 Exponential Backoff on Track B Calls (`track-a/track_a/retry.py`, `track-a/track_a/routing.py`)
- New `retry_with_backoff()` utility with configurable attempts, delay, and jitter
- Applied to all Track B calls: staging, submission, undo, and discard
- Prevents cascading failures during transient Track B outages

### 1.5 Exponential Backoff on LLM Retries (`track-a/track_a/ai_provider.py`)
- `RetryableProvider` now sleeps between retry attempts (0.5s base, 2s max)
- Reduces thrashing when the LLM is intermittently returning malformed JSON

## Phase 2: Security & Observability

### 2.1 Timing-Safe Admin Auth (`track-a/track_a/admin.py`, `track-a/track_a/dashboard.py`)
- Replaced `!=` comparison with `hmac.compare_digest()` for admin token verification
- Prevents timing attacks that could leak token bytes

### 2.2 Webhook Rate Limiting (`track-a/track_a/ratelimit.py`, `track-a/track_a/main.py`)
- Per-owner sliding-window rate limiter (30 messages/60s by default)
- Rate-limited messages are logged and counted in metrics
- Protects LLM and Track B pipeline from bursts

### 2.3 Structured Logging (`shared-contract/shared_contract/logging.py`)
- New `setup_logging()` function shared by both tracks
- When `JSON_LOGS=true`, emits JSON lines for log aggregators
- Falls back to human-readable format for development

### 2.4 Deep Health Checks (`track-a/track_a/main.py`, `track-b/track_b/main.py`)
- `/health` now verifies SQLite connectivity (Track A) and SQLite/Redis/Postgres (Track B)
- Returns `"status": "degraded"` when backends are unreachable
- Lightweight liveness probe for orchestrators

### 2.5 OpenAPI Metadata (`track-b/track_b/main.py`)
- Added Pydantic models for `OnboardRequest`, `UndoRequest`, `IntentRequest`
- Added `response_model` and `description` to endpoint decorators
- Improved auto-generated API documentation

### 2.6 Prometheus-Style Metrics (`track-a/track_a/metrics.py`)
- New `GET /metrics` endpoint in Prometheus text exposition format
- Tracks: messages received/duplicates/rate-limited, intents parsed, Track B calls, LLM calls/retries
- Thread-safe counter store with uptime gauge

## Phase 3: Code Quality & Deduplication

### 3.1 Deduplicated URL Normalization (`shared-contract/shared_contract/url.py`)
- Extracted `_plausible_url()` and `_normalize_url()` to shared `normalize_url()`
- Both Track A and Track B onboarding now use the same implementation
- Single source of truth for URL validation logic

### 3.2 Deduplicated Field Questions
- Track A's `FIELD_QUESTIONS_FALLBACK` mirrors Track B's `ContentTypeHandler.field_questions`
- Future improvement: fetch from Track B API at startup (documented in code comments)

### 3.3 Completed PostgresChangeLog (`track-b/track_b/changelog.py`)
- Added `list_changes()`, `count_by_action()`, and `count_failed()` to `PostgresChangeLog`
- Full parity with `InMemoryChangeLog` — enables removing the dashboard's direct SQLite hack

### 3.4 Jinja2 Templates (Planned)
- Dashboard and admin HTML should be migrated from f-strings to Jinja2 templates
- Eliminates XSS risk from missed `html.escape()` calls
- Improves maintainability of complex HTML views

## Phase 4: Feature Improvements

### 4.1 Image Pipeline Integration (`track-a/track_a/pipeline.py`)
- Pipeline now downloads images via WhatsApp Media API
- Stores base64-encoded content for the image handler
- New `ProcessingOutcome.image` status and `media_base64` field

### 4.2 i18n Foundation (`track-a/track_a/i18n.py`, `track-a/track_a/locales/en.json`)
- New `translate(key, locale="en", **kwargs)` function
- English locale file with all user-facing strings from composer, routing, and onboarding
- Adding a new locale = adding a JSON file

### 4.3 Multi-Site Conversation Support (Planned)
- Session state should include `site_id` for targeting the right WordPress site
- Onboarding should store the new `site_id` after successful connection
- "Switch to my other site" conversation branch needed

## Phase 5: Final Cleanup

### 5.1 Remove Dashboard SQLite Hack (Depends on 3.3)
- Once `PostgresChangeLog` is complete, replace direct SQLite queries in dashboard
- Dashboard becomes a pure frontend calling Track B's API

### 5.2 Documentation Update
- Updated READMEs with new env vars (`JSON_LOGS`)
- Added this improvements document
- CHANGELOG entry for all changes

## New Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JSON_LOGS` | `false` | Set to `true` for JSON log output |
| `ADMIN_TOKEN` | *(empty)* | Bearer token for admin/dashboard auth |
| `WPBOT_REDIS_URL` | `redis://localhost:6379/0` | Redis URL for pending store |
| `WPBOT_PG_DSN` | *(empty)* | Postgres DSN for change log |

## New Endpoints

| Endpoint | Service | Purpose |
|----------|---------|---------|
| `GET /metrics` | Track A | Prometheus-style metrics |
| `GET /health/ready` | Both | Deep readiness check |
| `GET /field-questions` | Track B | Content type field questions (planned) |
