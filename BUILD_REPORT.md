# Build Report — 2026-08-29 (Updated through Phase 1)

## 1. Phases completed
- Phase 0 (Audit): **DONE** — full codebase scan against PRODUCTION_SPEC_DETAILED.md and PRODUCTION_SPEC_APPENDIX.md. All 5 flagged decisions resolved by Justice. See §2–§6 of the original Phase 0 report.
- Phase 1 (Data model): **DONE** — 5 new tables created per §1, 33 new tests, 347 total passing. All existing 314 tests unaffected.
- Phase 2 (Reliability): NOT STARTED
- Phase 3 (Conversational state machine): NOT STARTED
- Phase 4 (Voice pipeline): NOT STARTED
- Phase 5 (WhatsApp migration): NOT STARTED
- Phase 6 (Infrastructure): NOT STARTED
- Phase 7 (Onboarding): NOT STARTED
- Phase 8 (Owner-facing features): NOT STARTED

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
| §6.2 Rate limiting | Fixed-window per-tenant in `rate_limit_buckets` table | In-memory sliding window | **Build per spec** | ✅ Done in Phase 1 — `check_and_increment_rate_limit()` |
| §6.3 Circuit breaker | Automatic `degraded` status, retry 3x, auto-recovery, operator alert | Basic exception catch-and-reply | **Build per spec** | Phase 2 |
| §1.2 Sessions | `conversation_sessions` table with state, pending_intent, context_history | In-memory `SessionStore` | **Build per spec** | ✅ Done in Phase 1 — `upsert_session()` with TTL expiry |
| §1.3 Action log | `action_log` with undone_at, source fields | `change_log` without undo tracking | **Build per spec** | ✅ Done in Phase 1 — `InMemoryActionLog` + `SQLiteActionLog` |
| §14 AI prompts | New prompt with `menu_item_add` schema, 6 few-shot examples | Old prompt with `create\|update\|delete` schema | **Extend** | Add new prompt alongside old; provider-swappable |
| §17 Error codes | `WP_UNREACHABLE`, `AUTH_FAILED`, `ENTITY_NOT_FOUND`, `PLUGIN_CONFLICT`, `UNKNOWN` | `WordPressError` with `status_code` only | **Build per spec** | New enum; map existing status_code to error codes |
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
| Circuit breaker (§6.3) | Not implemented | WP failures not handled gracefully | Build in Phase 2 |
| Multi-tenant isolation (§5) | ✅ Tables created | Not yet wired into routing pipeline | Wire in Phase 3 (state machine) |
| Rate limiting (§6.2) | ✅ DB-backed table created | Not yet wired into webhook handler | Wire in Phase 2 |
| Webhook idempotency (§6.1) | ✅ `processed_messages` table created | Not yet wired into webhook handler | Wire in Phase 2 |
| Conversation sessions (§1.2) | ✅ Table created | Not yet wired into router | Wire in Phase 3 (state machine) |
| Action log (§1.3) | ✅ Table + InMemory/SQLite implementations | Not yet wired into Track B apply flow | Wire in Phase 3 |
| Paystack (§9) | Not started | No payment integration | Out of scope for code audit |
| Dockerfile / Railway deploy | Not started | No production deployment path | Build in Phase 6 |
| Operator alerting (§7.4) | Not started | No degraded/fallback alerts | Build in Phase 6 |
| `page_content_update` action | Not implemented | Can't edit WP pages via bot | Build in Phase 1 (contract) + Phase 3 (state machine) |
| `unclear` action type | Not implemented (currently `unsupported` → escalation) | Spec wants `unclear` action, not escalation | Build in Phase 3 |

## 6. Phase 1 completion details

### Tables created

| Table | Spec § | Track | File | Purpose |
|---|---|---|---|---|
| `tenants` | §1.1 | A | `track-a/track_a/tenant_store.py` | Multi-tenant registry, sender_id → tenant lookup |
| `conversation_sessions` | §1.2 | A | `track-a/track_a/tenant_store.py` | Per-tenant conversation state with TTL expiry |
| `processed_messages` | §1.4 | A | `track-a/track_a/tenant_store.py` | Webhook idempotency (checked before AI call) |
| `rate_limit_buckets` | §1.5 | A | `track-a/track_a/tenant_store.py` | Fixed-window per-tenant rate limiting |
| `action_log` | §1.3 | B | `track-b/track_b/action_log.py` | Change log with undo support (undone_at, source) |

### Key functions (ready to wire in Phase 2–3)

**Track A (`tenant_store.py`):**
- `create_tenant()`, `get_tenant()`, `get_tenant_by_sender()` — tenant CRUD
- `update_tenant_status()`, `set_tenant_onboarded()` — status transitions for circuit breaker
- `upsert_session()`, `get_session()`, `clear_session()` — conversation state
- `mark_message_processed()` — idempotency gate (returns True if new, False if duplicate)
- `check_and_increment_rate_limit()` — fixed-window counter (returns is_limited, count)
- `cleanup_expired_sessions()`, `purge_old_processed_messages()` — TTL maintenance

**Track B (`action_log.py`):**
- `InMemoryActionLog` — deterministic test implementation
- `SQLiteActionLog` — pilot-scale persistence
- Both implement: `record()`, `most_recent()`, `mark_undone()`, `list_recent()`

### Test results
```
347 passed, 11 skipped in 29.92s
```
- 314 original tests: all passing (unchanged)
- 22 new tenant store tests: all passing
- 11 new action log tests: all passing

### What was NOT changed
- No existing tables modified
- No existing test files modified
- No existing module imports changed
- All new code is purely additive

## 7. Recommended next step

**Begin Phase 2 (Reliability).** Wire the new tables into the existing code paths:
1. Replace `wam_id` uniqueness check with `mark_message_processed()` before AI calls (§6.1)
2. Replace in-memory `RateLimiter` with `check_and_increment_rate_limit()` (§6.2)
3. Build the circuit breaker (§6.3) — retry logic, `update_tenant_status("degraded")`, auto-recovery, operator alerts
