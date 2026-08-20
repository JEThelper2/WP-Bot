# AI Provider Upgrade Runbook

Internal runbook for scaling the AI intent-parsing layer. This is a
**documentation-only decision guide** — no code changes are required
for the primary upgrade path (Groq free → Developer).

---

## When to Upgrade: The Trigger

The `FallbackChain` in `track_a/ai_provider` logs every rate-limit
failover. Watch for these log lines (logger: `track_a.ai_provider`):

```
WARNING  Provider GroqProvider rate-limited, falling back to GeminiProvider: ...
INFO     Fallback to GeminiProvider succeeded for this request
ERROR    Both GroqProvider and GeminiProvider rate-limited; failing over to low-confidence
```

### Upgrade Trigger Threshold

**Consider upgrading when fallback usage exceeds ~20 times per day
across all onboarded sites.**

Rationale: Groq's free tier allows ~30 requests/minute. A burst of
messages across multiple businesses can realistically hit this ceiling.
Occasional fallbacks (1–5/day) are normal and expected — the system is
designed to handle them. But a pattern of 20+ fallbacks/day means the
free tier's headroom is consistently exhausted and owners are
experiencing degraded response quality (Gemini fallback is slower and
has the training-data tradeoff).

### How to Monitor

```bash
# Count fallback events in the last 24 hours:
grep -c "falling back to" /var/log/wpbot/track-a.log  # or your log aggregator

# Count low-confidence failures from both providers being rate-limited:
grep -c "Both.*rate-limited" /var/log/wpbot/track-a.log
```

If you don't have centralized logging yet, a simple daily cron job
grep'ing the application log is sufficient during the pilot phase.

---

## Upgrade Path 1: Groq Developer Tier (Preferred)

This is the **lowest-friction upgrade path** — same provider, same
code, same API key. This is a deliberate design payoff from the
provider-agnostic architecture in Stage 1: because `GroqProvider`
communicates through Groq's OpenAI-compatible API and credentials are
env-driven, switching tiers is a billing change, not a code change.

### Steps

1. **Add billing to your existing Groq account**
   - Log into [console.groq.com](https://console.groq.com)
   - Navigate to Settings → Billing
   - Add a payment method
   - The Developer tier activates immediately; no restart needed

2. **Verify the new rate limits are in effect**
   - Check your current limits at [console.groq.com](https://console.groq.com)
     under Rate Limits
   - Developer tier: ~60+ requests/minute (vs. ~30 on free tier)
   - No code or config changes required — the same API key works

3. **Optionally relax the fallback trigger**
   - With increased headroom, the 20/day fallback threshold can be
     raised or the `AI_FALLBACK_PROVIDER` env var can be left in place
     as a safety net
   - Recommendation: keep the fallback configured even after upgrading —
     it costs nothing when not triggered and protects against spikes

4. **Monitor for 48 hours**
   - Confirm fallback frequency drops to near-zero
   - If fallbacks persist at the new tier, the issue is likely
     structured-output quality, not throughput (see Upgrade Path 2)

### What Does NOT Change

| Item | Status |
|------|--------|
| `AI_PROVIDER` env var | Same (`groq`) |
| `GROQ_API_KEY` env var | Same key, same value |
| `GroqProvider` code | Unchanged |
| `openai` SDK dependency | Unchanged |
| Default model (`openai/gpt-oss-120b`) | Unchanged |
| Fallback configuration | Optional — can keep or remove |

**This is the entire point of the Stage 1 architecture: scaling the
AI layer is a documented decision, not a fire drill.**

---

## Upgrade Path 2: Switch Primary Provider (Accuracy Issues)

If you observe **persistent structured-output or accuracy issues** —
not throughput — the Groq Developer tier won't help. Examples:

- Intent parsing consistently returns wrong `content_type` for clear
  messages
- `confidence` scores are systematically too high or too low
- JSON schema validation failures increase after a Groq model update
- Business owners report the bot "doesn't understand" common requests

These are quality issues, not capacity issues. In this case, consider
switching the primary provider to a stronger model.

### Recommended Alternatives

| Provider | Model | When to Consider |
|----------|-------|-----------------|
| **OpenAI** | GPT-4o-mini | Good balance of speed/quality; OpenAI-compatible API means minimal code changes |
| **Anthropic** | Claude Haiku | Strong instruction-following; would need a new `AIProvider` implementation |
| **Google** | Gemini 2.5 Pro | Already implemented as fallback; could promote to primary by changing `AI_PROVIDER=gemini` |

### Promotion (Config Change Only)

If the fallback provider (Gemini) has proven accurate enough:

```bash
# Promote Gemini to primary, remove fallback:
AI_PROVIDER=gemini
AI_FALLBACK_PROVIDER=
GEMINI_API_KEY=...
```

No code changes required — the provider-agnostic architecture handles
this automatically.

### New Provider (Code Required)

If adding Claude or another provider not yet implemented:

1. Write one class implementing `AIProvider` (see `GroqProvider` or
   `GeminiProvider` as templates)
2. Call `register_provider("claude", ClaudeProvider)` at import time
3. Set `AI_PROVIDER=claude` and `CLAUDE_API_KEY=...`
4. No changes to `IntentParser`, `FallbackChain`, or existing tests

---

## Decision Matrix

```
Is the issue throughput (rate limits) or quality (wrong intents)?
│
├── THROUGHPUT → Upgrade Path 1: Groq Developer Tier
│   └── Same key, same code, billing change only
│
└── QUALITY → Upgrade Path 2: Switch Primary Provider
    ├── Already have a fallback working? → Promote it (config only)
    └── Need a new provider? → Implement AIProvider (one new class)
```

---

## Cost Reference (as of August 2026)

| Provider | Tier | Input (per 1M tokens) | Output (per 1M tokens) | Rate Limit |
|----------|------|----------------------|------------------------|------------|
| Groq | Free | $0.15 | $0.60 | ~30 RPM |
| Groq | Developer | $0.15 | $0.60 | ~60+ RPM |
| Gemini | Free | Free | Free | 15 RPM, 1M TPD |
| OpenAI | Pay-as-you-go | $0.15 (GPT-4o-mini) | $0.60 | Varies |

At typical intent-parsing volumes (~100–500 requests/day), even the
Groq Developer tier costs under $1/month. The free tier is sufficient
for the pilot; upgrading is about reliability, not cost.

---

*Last updated: August 20, 2026*
*Owner: Justice*
*Review cycle: Revisit when onboarded sites exceed 10 or daily
intent-parsing requests exceed 1,000.*
