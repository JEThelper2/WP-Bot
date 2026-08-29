# Build Report — 2026-08-29 (Updated through Phase 7)

## 1. Phases completed
- Phase 0 (Audit): **DONE** — full codebase scan against PRODUCTION_SPEC_DETAILED.md and PRODUCTION_SPEC_APPENDIX.md. All 5 flagged decisions resolved by Justice. See §2–§5 of the original Phase 0 report.
- Phase 1 (Data model): **DONE** — 5 new tables created per §1, 33 new tests, 347 total passing. All existing 314 tests unaffected. Commit: 2a2aacc.
- Phase 2 (Reliability): **DONE** — reliability layer wired into request pipeline: idempotency (§6.1), DB-backed rate limiting (§6.2), circuit breaker with retry/backoff (§6.3), error code mapping (§17), owner-facing error messages. 32 new tests. Total: 379 passing, 11 skipped. Commit: 2f69812.
- Phase 3 (Conversational state machine): **DONE** — four-state machine (IDLE, AWAITING_CLARIFICATION, AWAITING_CONFIRMATION, EXECUTING) per §3. Non-destructive actions skip confirmation (§3.1); destructive actions go through AWAITING_CONFIRMATION. Template-based clarification (§3.4), exact confirmation word sets (§3.3), re-ask logic, undo matching (§3.5), context_history with timestamps for LLM re-entry (§3.2). 29+ tests. Total: 405 passing, 11 skipped. Commits: 4d533a8, 447e3c4.
- Phase 4 (Voice pipeline): **DONE** — §4.1 echo-back flow: always echo transcript, low-confidence caveat, affirmative/other reply handling. VOICE_AWAITING_ECHO state, proxy confidence, language_detected. 18 new tests. Total: 423 passing, 11 skipped. Commit: 4e764c3.
- Phase 5 (WhatsApp migration): **DONE** — audit confirmed WhatsApp is primary channel. Reply sender error handling, e2e WhatsApp flow tests, reliability verified with WhatsApp payloads. 10 new tests. Total: 433 passing, 11 skipped. Commit: 2cd17e8.
- Phase 6 (Infrastructure): **DONE** — Dockerfile, railway.json, Fernet secrets encryption, pre-commit key check, Telegram operator alerting, fallback frequency tracker. 17 new tests. Total: 450 passing, 11 skipped. Commit: f7456bd.
- Phase 7 (Onboarding): **DONE** — wired tenant_store into OnboardingFlow for §8 step 6-8 (create tenant record, flip to active, set onboarded_at). Fixed undo to work in IDLE session state (not just state is None). Added root conftest.py for cross-track test imports. 9 new onboarding smoke tests (full runbook, cancel, error paths, post-onboarding operations, multi-tenant isolation). Fixed end-to-end outbound test. Total: 461 passing, 11 skipped. Commit: 204aae9.
- Phase 8 (Owner-facing features): **DONE** — §10 Recap command: _is_recap() matching {recap, history, recent changes, what have i changed, what have i done, show my changes}. _handle_recap() queries Track B /changes endpoint, formats numbered list with relative timestamps (just now, X minutes ago, X hours ago, X days ago). Added /changes endpoint to Track B API, list_changes() to TrackBClient. §10 Draft/preview: compose_confirmation_with_diff() shows before/after diff for business_info_update ("Change your phone from 0801... to 0802..."). _stage_pending() passes before/after from Track B staging result to confirmation message. Locale keys added. 27 new tests. Total: 488 passing, 11 skipped. Commit: 8075aff.

## 2. Items that deviated from spec

| Spec section | Spec says | What exists | Decision | Rationale |
|---|---|---|---|---|
| §13 Content arch | ACF + `menu_item` CPT with `price`, `description`, `category`, `available` fields | Standard WP posts with `jobs`/`announcements` categories, no ACF | **Keep standard posts** | Simpler, no ACF dependency, already working in sandbox |
| §2 Intent schema | `action: menu_item_add\|menu_item_update\|menu_item_delete\|business_info_update\|page_content_update\|undo\|unclear`; `target: {entity_type, entity_id, entity_name_hint}`; `payload: {...}` | `action: create\|update\|delete`; `content_type: job\|announcement\|business_info\|image`; `fields: {...}` | **Extend, don't replace** | Add new action types alongside existing; no breaking changes to 314 tests |
| §2 Result schema | `{tenant_id, success, action_log_id, human_summary, error: {code, operator_detail, owner_message}}` | `{contract_version, status, change_id, before, after, live_url, error_message}` | **Extend spec's schema** | Add before/after/live_url for undo display; keep existing status enum |
| §15 Business info route | `wp-json/custom/v1/business-info` with ACF field names (`business_phone`, etc.) | `wp-json/wpbot/v1/business-info` with plain option keys (`phone`, `hours`, etc.) | **Keep wpbot/v1** | More descriptive namespace, already working |
| §16 mu-plugin PHP | ACF `get_field`/`update_field` with Options Page | Plain `get_option`/`update_option` | **Keep plain options** | No ACF dependency, simpler |
| §7.2 Secrets | AES-256-GCM | Fernet (AES-128-CBC + HMAC) | **Keep Fernet** | Already working; upgrade to AES-256-GCM later if needed |
| §3 State machine | `IDLE`, `AWAITING_CLARIFICATION`, `AWAITING_CONFIRMATION`, `EXECUTING` | `clarify`, `escalate`, `confirm` branches | **Extend** | Add `unclear` action type (replaces escalation); add IDLE as implicit default |
| §5 Multi-tenant | `tenants` table with UUID, `sender_id` lookup | Flat `owner_id`/`owner_phone` strings | **Build per spec** | ✅ Done in Phase 1 — `tenant_store.py` with full CRUD |
| §6.1 Idempotency | `processed_messages` table, unique constraint, checked before AI call | `wam_id UNIQUE` in `inbound_messages` | **Build per spec** | ✅ Done in Phase 1 — `mark_message_processed()` |
| §6.2 Rate limiting | Fixed-window per-tenant in `rate_limit_buckets` table | In-memory sliding window | **Build per spec** | ✅ Done in Phase 1, wired in Phase 2 — `DBRateLimiter` |
| §6.3 Circuit breaker | Automatic `degraded` status, retry 3x, auto-recovery, operator alert | Basic exception catch-and-reply | **Build per spec** | ✅ Done in Phase 2 — `CircuitBreaker` in reliability.py |
| §1.2 Sessions | `conversation_sessions` table with state, pending_intent, context_history | In-memory `SessionStore` | **Build per spec** | ✅ Done in Phase 1 — `upsert_session()` with TTL expiry |
| §1.3 Action log | `action_log` with undone_at, source fields | `change_log` without undo tracking | **Build per spec** | ✅ Done in Phase 1 — `InMemoryActionLog` + `SQLiteActionLog` |
| §14 AI prompts | New prompt with `menu_item_add` schema, 6 few-shot examples | Old prompt with `create\|update\|delete` schema | **Extend** | Add new prompt alongside old; provider-swappable |
| §17 Error codes | `WP_UNREACHABLE`, `AUTH_FAILED`, `ENTITY_NOT_FOUND`, `PLUGIN_CONFLICT`, `UNKNOWN` | `WordPressError` with `status_code` only | **Build per spec** | ✅ Done in Phase 2 — `classify_wp_error()` + `owner_message_for_error()` |
| §8.9 Smoke test | Automated smoke test during onboarding | Manual onboarding flow | **Build per spec** | New automation, not replacing existing flow |

## 3. Items flagged, not resolved

**None.** All 5 original flags have been resolved by Justice's decisions:

| Flag | Question | Decision |
|---|---|---|
| Flag 1 | ACF or standard posts? | Standard posts (swappable storage layer) |
| Flag 2 | Contract replacement vs extension? | Extend existing contract |
| Flag 3 | mu-plugin namespace? | Keep wpbot/v1 |
| Flag 4 | Result schema? | Extend with before/after/live_url |
| Flag 5 | Live test site? | Develop against fake WP; Phase 7 requires real site |

## 4. §20 confirmations (from appendix)

**Cannot be confirmed without a live WordPress site.** All 5 items deferred to Phase 7:

1. Whether `menu_item` is a CPT already → **N/A** — building against standard posts per decision. If a future site uses menu_item CPT, the storage layer is swappable.
2. Real ACF field names → **N/A** — not using ACF per decision. Business info uses plain WP options via mu-plugin.
3. Whether ACF REST exposure is enabled → **N/A** — not using ACF.
4. Whether Application Passwords are enabled → **YES in sandbox** (mu-plugin forces it via `wp_is_application_passwords_available` filter). Needs live verification in Phase 7.
5. Whether `menu_item` trash behavior is enabled → **N/A for standard posts** — standard WP posts trash by default. Verified in sandbox tests (test_wordpress.py::test_delete_post_moves_to_trash passes).

## 5. Untested / risk areas

| Area | Status | Risk | Mitigation |
|---|---|---|---|
| Voice transcription (§4.2) | Synthetic English only | Nigerian-accented/Pidgin/Yoruba accuracy unknown | Mandatory echo-back step (§4.1.3) is the mitigation |
| WhatsApp Business API | Code paths exist, no real Meta credentials | Cannot verify real delivery | Telegram adapter covers conversation flow; WhatsApp-specific in Phase 5 |
| Multi-tenant isolation (§5) | ✅ Tables created, wired into webhook | Routing pipeline uses owner_id, not tenant_id yet | Acceptable for pilot (single-tenant mode) |
| Rate limiting (§6.2) | ✅ DB-backed, wired into webhook | Falls back to in-memory for pre-onboarding tenants | Acceptable for pilot |
| Webhook idempotency (§6.1) | ✅ `processed_messages` table, wired into webhook | Falls back to `wam_id` uniqueness for pre-onboarding tenants | Acceptable for pilot |
| Circuit breaker (§6.3) | ✅ Implemented and wired into routing.py | Tested with synthetic WP errors | Needs live failure scenario test in Phase 7 |
| Conversation sessions (§1.2) | ✅ Table created, wired into router via state machine | In-memory SessionStore used (SQLite-backed table available) | Swap to SQLite-backed store for multi-worker in Phase 6 |
| Action log (§1.3) | ✅ Table + InMemory/SQLite implementations | Not yet wired into Track B apply flow | Wire in Phase 5+ |
| State machine (§3) | ✅ Four states, re-ask logic, exact word sets, context_history | Tested against scripted parser only | Live LLM test in Phase 7 |
| Paystack (§9) | Not started | No payment integration | Out of scope for code audit |
| Dockerfile / Railway deploy | Not started | No production deployment path | Build in Phase 6 |
| Operator alerting (§7.4) | Alert callback wired into CircuitBreaker | No Telegram sender connected to alert yet | Build in Phase 6 |
| `page_content_update` action | Not implemented | Can't edit WP pages via bot | Build in Phase 5+ (WhatsApp migration)
| `unclear` action type | ✅ Unsupported → AWAITING_CLARIFICATION with template question | Replaces escalation per §3 | Done in Phase 3 |

## 6. Phase 2 completion details

### Components implemented

| Component | Spec section | Implementation | File |
|---|---|---|---|
| Idempotency | §6.1 | `IdempotencyChecker` wraps `processed_messages` table | `track-a/track_a/reliability.py` |
| Rate limiting | §6.2 | `DBRateLimiter` wraps `rate_limit_buckets` table (30 msg/hr/tenant) | `track-a/track_a/reliability.py` |
| Circuit breaker | §6.3 | `CircuitBreaker` wraps Track B calls (3x retry, 1s/3s/9s backoff) | `track-a/track_a/reliability.py` |
| Error mapping | §17 | `classify_wp_error()` maps WP REST errors to internal codes | `track-a/track_a/reliability.py` |
| Owner messages | §6.3 | `owner_message_for_error()` returns plain-language messages | `track-a/track_a/reliability.py` |
| Operator alerts | §7.4 | Alert callback on transition to degraded (not repeated failures) | `track-a/track_a/reliability.py` |

### Wiring

- **Webhook handler** (`main.py`): Resolves tenant_id from sender_id, uses `IdempotencyChecker` + `DBRateLimiter` when tenant exists, falls back to legacy in-memory limiter
- **Routing layer** (`routing.py`): `_trackb_call()` wraps `submit_intent` and `undo` with `CircuitBreaker`; maps `CircuitBreakerError` to owner-facing messages

### Test results
```
379 passed, 11 skipped in 51.08s
```
- 314 original tests: all passing (unchanged)
- 33 Phase 1 tests: all passing (unchanged)
- 32 Phase 2 tests: all passing (new)

### What was NOT changed
- No existing tables modified
- No existing test files modified (only new test file added)
- All new code is purely additive

## 7. Phase 3 completion details

### Components implemented

| Component | Spec section | Implementation | File |
|---|---|---|---|
| State machine | §3.1 | Four states: IDLE, AWAITING_CLARIFICATION, AWAITING_CONFIRMATION, EXECUTING | `track-a/track_a/session.py` |
| Confirmation matching | §3.3 | Exact word sets: yes/yeah/yep/confirm/ok/okay/go ahead/do it; no/nope/cancel/stop/don't | `track-a/track_a/routing.py` |
| Re-ask logic | §3.3 | First ambiguous reply → re-ask; second → cancel and return to IDLE | `track-a/track_a/routing.py` |
| Undo matching | §3.5 | Exact word set: undo/undo that/undo last change/revert | `track-a/track_a/routing.py` |
| Template clarification | §3.4 | Template-based questions for missing entities, missing fields, low confidence | `track-a/track_a/routing.py` + `locales/en.json` |
| Context history | §3.2 | Exchange transcript (last 6 turns) passed to LLM on clarification re-entry | `track-a/track_a/session.py` + `routing.py` |
| Unclear handling | §3.1 | unsupported → AWAITING_CLARIFICATION with template question (replaces escalate) | `track-a/track_a/routing.py` |

### Bug fix

- **SessionStore falsy bug**: `SessionStore.__len__` returns 0 for empty stores, making `sessions or SessionStore()` in `IntentRouter.__init__` always create a default store, ignoring the passed-in instance. Fixed with `sessions if sessions is not None else SessionStore()`.

### Test results
```
450 passed, 11 skipped in 45.43s
```
- 314 original tests: all passing (unchanged)
- 33 Phase 1 tests: all passing (unchanged)
- 32 Phase 2 tests: all passing (unchanged)
- 29 Phase 3 tests: all passing
- 18 Phase 4 tests: all passing
- 10 Phase 5 tests: all passing
- 17 Phase 6 tests: all passing (new)
- Updated integration tests: 5 passing
- 4 pre-existing skipped (wp_fake import, Redis TTL timing)

### What was NOT changed
- No existing tables modified
- Intent schema unchanged (no new action types needed for state machine)
- Track B code unchanged
- Onboarding flow unchanged (operates as side channel outside state machine)

## 8. Final build status

**All 8 phases implemented. 488 tests passing, 11 skipped.**

### Per-phase test counts
- Phase 1 (Data model): 33 new tests
- Phase 2 (Reliability): 32 new tests
- Phase 3 (State machine): 29 new tests
- Phase 4 (Voice pipeline): 18 new tests
- Phase 5 (WhatsApp): 10 new tests
- Phase 6 (Infrastructure): 17 new tests
- Phase 7 (Onboarding): 9 new tests
- Phase 8 (Owner features): 27 new tests
- Pre-existing tests: 314 (unaffected)

### Known untested areas (require real infrastructure)
1. Voice transcription accuracy on Nigerian Pidgin/Yoruba/Igbo/Hausa (§4.2)
2. Live WordPress site onboarding runbook (§8)
3. Railway deployment end-to-end
4. Real WhatsApp Business API delivery
5. ENCRYPTION_KEY production setup (§7.2)

### Recommended next step

Execute the onboarding runbook (§8) on a real WordPress test site with a real WhatsApp Business API phone number to validate the full production flow end-to-end.
