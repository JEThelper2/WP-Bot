"""Track B STUB app (kept for Track A's pre-Integration-Phase tests).

The real API lives in `track_b.main.create_app`. This stub still answers
`POST /intent` with a canned contract-valid success result, so Track A's
conversation flow stays testable end-to-end against the contract shapes
until the Integration Phase points Track A at the real endpoint.
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


def create_stub_app() -> FastAPI:
    app = FastAPI(
        title="WP-Bot Track B (WordPress site/state) — STUB",
        version="0.1.0",
        description=(
            "Stub Track B. POST /intent returns a canned success result so "
            "Track A is testable against the real contract shapes."
        ),
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "track-b"}

    @app.post("/intent")
    async def apply_intent(payload: dict[str, Any] = Body(...)) -> JSONResponse:
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
        validate_result(result)
        return JSONResponse(result, status_code=200)

    return app


stub_app = create_stub_app()
