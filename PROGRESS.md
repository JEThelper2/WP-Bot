# Build Progress — WhatsApp-to-WordPress SaaS

Last updated: 2026-08-29

## Phase status
- Phase 0 (Audit): DONE
- Phase 1 (Data model): DONE
- Phase 2 (Reliability): DONE
- Phase 3 (Conversational state machine): DONE
- Phase 4 (Voice pipeline): DONE
- Phase 5 (WhatsApp migration): DONE
- Phase 6 (Infrastructure): DONE
- Phase 7 (Onboarding): DONE
- Phase 8 (Owner-facing features): DONE

## Currently in progress
All 8 phases + session isolation fix complete. Ready for real WordPress site testing.

## Phase 3 audit — §3 compliance checklist

### §3.1 States (4 states)
- ✅ IDLE: no session stored → next message parsed fresh
- ✅ AWAITING_CLARIFICATION: session.state = "AWAITING_CLARIFICATION", exchange populated
- ✅ AWAITING_CONFIRMATION: session.state = "AWAITING_CONFIRMATION", pending_intent set
- ✅ EXECUTING: transient — submit_pending executes inline, clears session on completion

### §3.1 Transitions
- ✅ IDLE → AWAITING_CLARIFICATION: low confidence (< 0.75), missing required field, or unsupported/unclear
- ✅ IDLE → AWAITING_CONFIRMATION: destructive action (delete, business_info update) with high confidence
- ✅ IDLE → IDLE (non-destructive): create, non-business_info update skip confirmation, execute immediately
- ✅ AWAITING_CLARIFICATION → AWAITING_CONFIRMATION: owner resolves ambiguity (re-parse with context)
- ✅ AWAITING_CLARIFICATION → IDLE: max clarification turns (3) exceeded → "still unsure" message
- ✅ AWAITING_CONFIRMATION → EXECUTING: owner replies affirmative
- ✅ AWAITING_CONFIRMATION → IDLE: owner replies negative → cancel, discard staged intent
- ✅ EXECUTING → IDLE: action completes (success or failure), session cleared
- ✅ Any state → IDLE: session expiry (15 min TTL via SessionStore)

### §3.2 Context history
- ✅ Last 6 turns stored (capped via turns[-6:])
- ✅ Each entry has role, text, and at (ISO timestamp)
- ✅ Formatted and passed to parser as context on clarification re-entry
- ✅ Fresh message resets expires_at = now + 15 minutes

### §3.3 Confirmation matching
- ✅ Affirmative: {yes, yeah, yep, confirm, ok, okay, go ahead, do it} — exact set, case-insensitive
- ✅ Negative: {no, nope, cancel, stop, dont, don't} — exact set, case-insensitive
- ✅ Ambiguous (not yes/no) → re-ask once ("Sorry, should I go ahead? Reply yes or no.")
- ✅ Second ambiguous reply → cancel + return to IDLE ("Okay, the request has been cancelled.")
- ✅ Re-ask count tracked via session.re_ask_count

### §3.4 Clarification templates
- ✅ Missing entity: targeted_question() from _FIELD_QUESTION_KEYS
- ✅ Missing field: field-specific question (e.g. "What's the job title?")
- ✅ Low confidence with complete intent: confirm_low_confidence summary
- ✅ Unsupported/unclear: no_intent_question template ("Sorry — I couldn't quite understand...")
- ✅ Max turns exceeded: still_unsure template

### §3.5 Undo
- ✅ Undo matching: {undo, undo that, undo last change, revert} — exact set, case-insensitive
- ✅ Undo when no confirmation pending (state is None or IDLE)
- ✅ Track B undo() called with owner_id and optional site_id
- ✅ Undo result relayed as completion or error message

### §3 Edge cases
- ✅ Empty message → low confidence → clarification
- ✅ Session expiry: lazy eviction on get(), TTL-based
- ✅ Pending intent with no state → error reply + session clear
- ✅ Duplicate webhook delivery → idempotency (§6.1, tested in Phase 2)
- ✅ Rate limiting → drop message (§6.2, tested in Phase 2)

### Known deviations from §3 spec
1. **page_content_update**: Not yet implemented (out of scope for Phase 3, flagged for Phase 5+)
2. **Confirmation staging**: Intent staged at Track B before confirmation prompt (Integration Phase design). Spec doesn't specify staging, but this prevents stale-intent writes.
3. **Escalation removed**: Spec has no escalate branch. Replaced with unclear → AWAITING_CLARIFICATION. Escalation logging retained for potential future developer handoff.
4. **Confidence threshold**: 0.75 (spec says < 0.7 triggers clarification — our threshold is slightly higher for safety)

## Completed this build (append-only log, do not delete old entries)
- [2026-08-29] Pre-spec work: fixed admin.py NameError (_escap/_status_badge), lazy app init, Telegram webhook auth, RateLimiter thread safety, SQLite connection leaks, N+1 query in list_sites, count_failed changelog logging, routing escalation bug, has_edit_permissions fix, stale test assertions. 314 tests pass. Commit: 8a50535.
- [2026-08-29] Phase 0: Full audit against PRODUCTION_SPEC_DETAILED.md and PRODUCTION_SPEC_APPENDIX.md. Gap report produced. BUILD_REPORT.md written. All 5 flagged decisions resolved by Justice. Phase 0 marked DONE.
- [2026-08-29] Phase 1: Created 5 new data model tables per §1. Track A: tenants (§1.1), conversation_sessions (§1.2), processed_messages (§1.4), rate_limit_buckets (§1.5). Track B: action_log (§1.3) with InMemoryActionLog and SQLiteActionLog. 33 new tests. Total: 347 passing, 11 skipped. Commit: 2a2aacc.
- [2026-08-29] Phase 2: Wired §6 reliability into request pipeline. New reliability.py: IdempotencyChecker (§6.1 processed_messages), DBRateLimiter (§6.2 rate_limit_buckets), CircuitBreaker (§6.3 retry+backoff+tenant status). Error code mapping (§17) with owner-facing messages. Circuit breaker wraps Track B calls in routing.py (submit_intent, undo). Webhook handler uses DB-backed idempotency+rate limiting when tenant exists, falls back to legacy mode. 32 new tests. Total: 379 passing, 11 skipped. Commit: 2f69812.
- [2026-08-29] Phase 3: Conversational state machine per §3. Four states (IDLE, AWAITING_CLARIFICATION, AWAITING_CONFIRMATION, EXECUTING). Replaced escalate with unclear → AWAITING_CLARIFICATION (§3.4 templates). Tightened _is_yes/_is_no to exact spec word sets (§3.3). Added re-ask logic: first ambiguous reply re-asks, second cancels (§3.3). Added _is_undo matching per §3.5. Updated context_history (§3.2) for LLM re-entry with ISO timestamps. Added confirmation_reask/confirmation_cancelled locale keys. Fixed IntentRouter.__init__ falsy SessionStore bug. Spec compliance fix: non-destructive actions (create, non-business_info update) skip confirmation and execute immediately; only destructive actions (delete, business_info update) go through AWAITING_CONFIRMATION. 29+ integration tests updated. Total: 405 passing, 11 skipped. Commits: 4d533a8, 447e3c4.
- [2026-08-29] Phase 4: Voice pipeline per §4. §4.1 step 3: always echo transcript back (hard rule, not conditional on confidence). §4.1 step 4: low-confidence (< 0.5) prepends caveat. §4.1 step 5: affirmative → use transcript; other → treat as corrected text. VOICE_AWAITING_ECHO state added to session. source="voice" on handle_message triggers echo flow. Proxy confidence calculation (word_count / (duration * 2)). language_detected field on Transcription. GroqTranscriber populates language. Webhook handler passes source="voice" for audio messages. 18 new tests. Total: 423 passing, 11 skipped. Commit: 4e764c3.
- [2026-08-29] Phase 5: WhatsApp migration audit and verification. Confirmed WhatsApp is primary channel (webhook, reply sender, media client, voice pipeline all implemented). WhatsAppReplySender now handles HTTP errors gracefully. Added reply sender error tests (429, connection error). Added end-to-end WhatsApp flow tests (webhook → pipeline → router → reply). Reliability tests verified with WhatsApp payloads (duplicate detection, rate limiting). Multi-tenant tests verified with WhatsApp sender_id resolution. Total: 433 passing, 11 skipped. Commit: 2cd17e8.
- [2026-08-29] Phase 6: Infrastructure per §7. Dockerfile for Railway (Python 3.12-slim). railway.json with health check. Fernet-based secrets encryption for WordPress Application Passwords (§7.2). Tenant store encrypts on write, decrypts at API call point. Pre-commit check for leaked API key patterns (sk-, AIza, ghp_, AKIA). TelegramAlertSender for operator notifications (§7.4). FallbackFrequencyTracker for AI provider monitoring. 17 new tests. Total: 450 passing, 11 skipped. Commit: f7456bd.
- [2026-08-29] Phase 7: Onboarding per §8. Wired tenant_store into OnboardingFlow for §8 step 6-8 (create tenant, flip to active, set onboarded_at). Added root conftest.py for cross-track test imports. Fixed undo check to work in IDLE session state (not just state is None). Added 9 onboarding smoke tests covering full runbook flow, cancel, invalid URL, wrong password, insufficient permissions, post-onboarding text/undo/failure, and multi-tenant isolation. Fixed end-to-end outbound test (pre-seeded WordPress post for delete). Total: 461 passing, 11 skipped. Commit: 204aae9.
- [2026-08-29] Phase 8: Owner-facing features per §10. §10 Recap command: _is_recap() matching {recap, history, recent changes, what have i changed, what have i done, show my changes}. _handle_recap() queries Track B /changes endpoint, formats numbered list with relative timestamps. Added /changes endpoint to Track B, list_changes() to TrackBClient. §10 Draft/preview: compose_confirmation_with_diff() shows before/after diff for business_info_update. _stage_pending() passes before/after from Track B staging result. Locale keys: recap_header, recap_entry, recap_empty, recap_undo_entry, compose_business_info_update_with_before. 27 new tests. Total: 488 passing, 11 skipped. Commit: 8075aff.

## Decisions resolved (from Phase 0 audit — 2026-08-29)
1. **Content architecture (§13)**: Build against standard posts + categories (what exists in sandbox). Content-storage layer is swappable — no ACF dependency.
2. **Contract (§2)**: Extend existing contract with new action types alongside existing ones. No breaking changes.
3. **Result schema (§2)**: Extend spec's result schema with before/after/live_url fields for undo display.
4. **mu-plugin (§16)**: Keep wpbot/v1 namespace (existing).
5. **Test site**: Develop against FakeWordPress. Phase 7 flagged as requiring a real site.

## Deviations from spec (mirrors §19's "Items that deviated from spec")
1. Content types: standard posts + categories instead of ACF + menu_item CPT (per Justice's decision)
2. Contract: extended, not replaced (per Justice's decision)
3. Result schema: extended with before/after/live_url (per Justice's decision)
4. mu-plugin namespace: wpbot/v1 instead of custom/v1 (per Justice's decision)
5. Escalation branch: replaced with unclear → AWAITING_CLARIFICATION per §3 (spec has no escalate branch)
6. Confirmation matching: tightened to exact spec word sets (§3.3), removing previously permissive matches
7. Confidence threshold: 0.75 vs spec's 0.7 (slightly higher for safety)
8. Voice language audit: not yet tested on real Nigerian Pidgin/Yoruba/Igbo/Hausa samples (requires real voice notes)
9. Live test site: FakeWordPress only; Phase 7 onboarding runbook not executed on real WordPress site

## Final build audit — 2026-08-29

**Test results: 488 passed, 11 skipped (1 pre-existing timing test, 4 pre-existing import skips, 6 Redis-related skips)**

### Per-phase verification
| Phase | Status | Verified against |
|---|---|---|
| Phase 0 (Audit) | DONE | Full codebase scan against PRODUCTION_SPEC_DETAILED.md + APPENDIX.md |
| Phase 1 (Data model) | DONE | 4 Track A tables (tenants, conversation_sessions, processed_messages, rate_limit_buckets) + 1 Track B table (action_log) confirmed in schema |
| Phase 2 (Reliability) | DONE | IdempotencyChecker (§6.1), DBRateLimiter (§6.2), CircuitBreaker with retry/backoff (§6.3), ReliabilityLayer, error code mapping (§17) |
| Phase 3 (State machine) | DONE | 4 states (IDLE, AWAITING_CLARIFICATION, AWAITING_CONFIRMATION, EXECUTING), all transitions, exact confirmation word sets (§3.3), re-ask logic, undo (§3.5), context_history with timestamps (§3.2), template-based clarification (§3.4) |
| Phase 4 (Voice pipeline) | DONE | VOICE_AWAITING_ECHO state, echo-back (§4.1 step 3), low-confidence caveat (§4.1 step 4), correction handling (§4.1 step 5), proxy confidence calculation, language_detected |
| Phase 5 (WhatsApp) | DONE | WhatsApp primary channel confirmed. WhatsAppReplySender, WhatsAppMediaClient, webhook verification, error handling |
| Phase 6 (Infrastructure) | DONE | Dockerfile (Railway), railway.json, Fernet secrets encryption (§7.2), pre-commit key check, TelegramAlertSender (§7.4), FallbackFrequencyTracker |
| Phase 7 (Onboarding) | DONE | OnboardingFlow wires tenant_store for §8 step 6-8, 9 smoke tests cover full runbook, cancel, error paths, multi-tenant isolation |
| Phase 8 (Owner features) | DONE | §10 Recap command (exact word set, Track B /changes endpoint, numbered list with relative timestamps), §10 Draft/preview (before/after diff for business_info_update in confirmation message) |

### Known untested areas (require real infrastructure)
1. Voice transcription accuracy on Nigerian-accented English, Pidgin, Yoruba/Igbo/Hausa (§4.2)
2. Live WordPress site onboarding runbook execution (§8)
3. Railway deployment end-to-end
4. Real WhatsApp Business API webhook delivery and response timing
5. ENCRYPTION_KEY setup on Railway for production secrets (§7.2)

### Gaps fixed after build audit
- **Session isolation (§1.2)**: Router now uses DBSessionStore backed by conversation_sessions table, keyed by tenant_id. Previously used in-memory SessionStore keyed by owner_id — all sessions lost on restart, no tenant isolation at the session layer. Fixed in commit ca6ae8f.
- **page_content_update**: Still not implemented — no content type in Track B registry. Not planned for any phase.
