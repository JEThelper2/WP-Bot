# Build Progress — WhatsApp-to-WordPress SaaS

Last updated: 2026-08-29

## Phase status
- Phase 0 (Audit): DONE
- Phase 1 (Data model): DONE
- Phase 2 (Reliability): DONE
- Phase 3 (Conversational state machine): DONE
- Phase 4 (Voice pipeline): NOT STARTED
- Phase 5 (WhatsApp migration): NOT STARTED
- Phase 6 (Infrastructure): NOT STARTED
- Phase 7 (Onboarding): NOT STARTED
- Phase 8 (Owner-facing features): NOT STARTED

## Currently in progress
Phase 3 complete. Ready for Phase 4 (Voice pipeline: §4 transcription, echo-back, proxy confidence).

## Completed this build (append-only log, do not delete old entries)
- [2026-08-29] Pre-spec work: fixed admin.py NameError (_escap/_status_badge), lazy app init, Telegram webhook auth, RateLimiter thread safety, SQLite connection leaks, N+1 query in list_sites, count_failed changelog logging, routing escalation bug, has_edit_permissions fix, stale test assertions. 314 tests pass. Commit: 8a50535.
- [2026-08-29] Phase 0: Full audit against PRODUCTION_SPEC_DETAILED.md and PRODUCTION_SPEC_APPENDIX.md. Gap report produced. BUILD_REPORT.md written. All 5 flagged decisions resolved by Justice. Phase 0 marked DONE.
- [2026-08-29] Phase 1: Created 5 new data model tables per §1. Track A: tenants (§1.1), conversation_sessions (§1.2), processed_messages (§1.4), rate_limit_buckets (§1.5). Track B: action_log (§1.3) with InMemoryActionLog and SQLiteActionLog. 33 new tests. Total: 347 passing, 11 skipped. Commit: 2a2aacc.
- [2026-08-29] Phase 2: Wired §6 reliability into request pipeline. New reliability.py: IdempotencyChecker (§6.1 processed_messages), DBRateLimiter (§6.2 rate_limit_buckets), CircuitBreaker (§6.3 retry+backoff+tenant status). Error code mapping (§17) with owner-facing messages. Circuit breaker wraps Track B calls in routing.py (submit_intent, undo). Webhook handler uses DB-backed idempotency+rate limiting when tenant exists, falls back to legacy mode. 32 new tests. Total: 379 passing, 11 skipped. Commit: 2f69812.
- [2026-08-29] Phase 3: Conversational state machine per §3. Four states (IDLE, AWAITING_CLARIFICATION, AWAITING_CONFIRMATION, EXECUTING). Replaced escalate with unclear → AWAITING_CLARIFICATION (§3.4 templates). Tightened _is_yes/_is_no to exact spec word sets (§3.3). Added re-ask logic: first ambiguous reply re-asks, second cancels (§3.3). Added _is_undo matching per §3.5. Updated context_history (§3.2) for LLM re-entry. Added confirmation_reask/confirmation_cancelled locale keys. Fixed IntentRouter.__init__ falsy SessionStore bug. 29 new tests. Total: 403 passing, 11 skipped. Commit: 4d533a8.

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
