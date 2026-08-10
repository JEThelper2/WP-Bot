"""WP-Bot Track B — WordPress site/state service (STUB).

For now this only answers `POST /intent` with a canned success result so
that Track A has a real contract-shaped endpoint to call against. It
performs the boundary validation required by the shared contract:
reject intents that fail intent.schema.json, and never emit a result
that fails result.schema.json.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse
from shared_contract import (
    CONTRACT_VERSION,
    ContractValidationError,
    validate_intent,
    validate_result,
)


def _stub_change_id() -> str:
    return f"stub-{uuid.uuid4().hex[:12]}"


def create_app() -> FastAPI:
    app = FastAPI(
        title="WP-Bot Track B (WordPress site/state) — stub",
        version="0.1.0",
        description=(
            "Stub Track B service. POST /intent validates the intent object and "
            "returns a canned success result. Later this becomes the real WordPress "
            "site/state service."
        ),
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "track-b"}

    @app.post("/intent")
    async def apply_intent(payload: dict[str, Any] = Body(...)) -> JSONResponse:
        # Boundary validation #1: never trust Track A — re-validate the intent.
        try:
            validate_intent(payload)
        except ContractValidationError as exc:
            failed: dict[str, Any] = {
                "contract_version": CONTRACT_VERSION,
                "status": "failed",
                "change_id": _stub_change_id(),
                "before": None,
                "after": None,
                "live_url": None,
                "error_message": f"intent rejected at boundary: {exc}",
            }
            # Boundary validation #2: what we emit must itself be contract-valid.
            validate_result(failed)
            return JSONResponse(failed, status_code=422)

        result: dict[str, Any] = {
            "contract_version": CONTRACT_VERSION,
            "status": "success",
            "change_id": _stub_change_id(),
            "before": None,
            "after": payload["fields"],
            "live_url": f"https://example.com/owners/{payload['owner_id']}",
            "error_message": None,
        }
        # Boundary validation #2: what we emit must itself be contract-valid.
        validate_result(result)
        return JSONResponse(result, status_code=200)

    return app


app = create_app()
