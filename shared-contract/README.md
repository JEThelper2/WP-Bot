# WP-Bot Shared Contract

This directory is the **only coupling point** between the two WP-Bot services:

| Service | Role |
|---|---|
| **Track A** — WhatsApp conversation service | Understands the owner's plain-language message and produces an **intent object**. |
| **Track B** — WordPress site/state service | Knows the site and its content state, applies the intent, and returns a **result object**. |

```
Business owner
    │  WhatsApp message ("change my hours to 9-6")
    ▼
Track A (conversation service)
    │  intent object  ─────────────►  validate against intent.schema.json
    ▼
Track B (WordPress service)
    │  result object  ◄────────────  validate against result.schema.json
    ▼
Track A (replies to the owner)
```

Neither service ever needs to change because of the other's **internals**.
Only this contract can change — and when it does, **both tracks update
together in the same change**.

## Files

| File | Purpose |
|---|---|
| `intent.schema.json` | JSON Schema (draft 2020-12) for the intent object, Track A → Track B. |
| `result.schema.json` | JSON Schema (draft 2020-12) for the result object, Track B → Track A. |
| `shared_contract/` | Python package both services import to validate at their boundary. |
| `pyproject.toml` | Package metadata so both services can install it (`wpbot-shared-contract`). |
| `README.md` | This document. |

## The intent object (Track A → Track B)

```jsonc
{
  "contract_version": "1.0.0",          // required, must match both services
  "owner_id": "string",                  // required, stable business/site id
  "action": "create | update | delete",  // required
  "content_type": "job | announcement | business_info | image",  // required
  "fields": { /* shape depends on content_type */ },             // required
  "confidence": 0.0 - 1.0                // required
}
```

`fields` is validated against per-content-type sub-schemas (`$defs` in the schema):

| content_type | fields | Notes |
|---|---|---|
| `job` | `title`, `description`, `location`, `remote` (bool), `category` | `create` requires `title` + `description`; `update`/`delete` accept a partial set (≥ 1 field). |
| `announcement` | `title`, `body`, `expires_at` (optional, RFC 3339 date-time) | `create` requires `title` + `body`. |
| `business_info` | `phone`, `hours`, `address`, `prices` | **All optional** — partial updates are the intended use ("change my hours to 9-6" sends only `hours`). At least one field required. |
| `image` | `slot` (`homepage_banner` \| `logo` \| `gallery`), plus `media_url` **or** `media_base64` | **v1.5 feature — not required for the v1 MVP** (see below). Exactly one of `media_url`/`media_base64` for `create`/`update`; neither for `delete` (`slot` identifies the image). |

## The result object (Track B → Track A)

```jsonc
{
  "contract_version": "1.0.0",          // required, must match both services
  "status": "success | failed | needs_confirmation",  // required
  "change_id": "string",                 // required, stable per change
  "before": { } | null,                  // previous state (null for create)
  "after":  { } | null,                  // new state (null for delete)
  "live_url": "string" | null,           // URL where the change is live
  "error_message": "string" | null       // required, non-null when status = failed; must be null on success
}
```

`needs_confirmation` is how Track B asks Track A to go back to the owner
("I'll change the hours to 9-6 — confirm?"). Track A echoes `change_id` back
in the follow-up intent so Track B knows which pending change to apply.

## The golden rules

1. **Both sides validate.** Track A validates the intent it builds *before*
   sending, and Track B re-validates it *before* doing anything. Track B
   validates the result it builds, and Track A re-validates every result it
   receives. Never trust the other side — the boundary check is the last
   line of defense.
2. **Version in lockstep.** Both schemas carry `contract_version: "1.0.0"`.
   The validator rejects anything with a different version, so the two
   services can never silently disagree about the contract.
3. **Schema changes are shared work.** Any change to either schema is a
   change to *both* tracks. Bump `contract_version` (semver), update the
   package version in `pyproject.toml`, update both services in the **same**
   change, and notify the other track's maintainers before it lands.
4. **Strict by default.** `additionalProperties` is `false` and unknown
   fields are rejected — the contract is a closed set. To add a field, follow
   rule 3.
5. **`image` is v1.5.** The `image` content_type (and its `image_fields`
   sub-schema) is marked `"x-v1-mvp": false` and `"x-introduced-in": "1.5"`.
   Track B is **not** required to implement it in the v1 MVP; the schema
   validates it anyway so both tracks can develop against the final shape.
   Drop it from the `content_type` enum if it becomes a blocker before v1.5.

## Using the validator

Both services declare the package as a path dependency:

```bash
pip install -e ./shared-contract
```

```python
from shared_contract import (
    CONTRACT_VERSION,
    ContractValidationError,
    validate_intent,
    validate_result,
)

try:
    validate_intent(payload)          # returns payload unchanged on success
except ContractValidationError as exc:
    # e.g. "intent failed validation at $.fields.title: 'title' is a required property"
    print(exc)
```

Errors always name the exact field that failed (JSON pointer style, e.g.
`$.fields.remote`), so both tracks can log or surface them without parsing
the other side's internals.
