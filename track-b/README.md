# WP-Bot Track B — WordPress site/state service (stub)

For now this service only answers `POST /intent` with a **canned success
result** so Track A has a real, contract-shaped endpoint to call against.
It performs the boundary validation required by `shared-contract/`:

- re-validates every intent it receives (`validate_intent`), rejecting bad
  ones with a contract-valid `status: "failed"` result (HTTP 422);
- validates every result it emits (`validate_result`) before returning it.

## Run

```bash
# from the repo root (installs shared-contract, track-a, track-b)
pip install -e ./shared-contract -e ./track-a -e ./track-b
uvicorn track_b.main:app --port 8200
```

| Endpoint | Behavior |
|---|---|
| `GET /health` | Liveness check. |
| `POST /intent` | Accepts an intent object; returns a canned success result (`change_id` `stub-*`, `after` = the intent's `fields`). |

## Test

```bash
pytest track-b/tests
```
