"""WP-Bot Track B — WordPress site/state service.

- `POST /intent` (STUB) — canned contract-valid success result, so Track
  A's full conversation flow is testable before Track B applies real
  writes. The real write path (B1 client + B2 gate + change log + undo)
  is built and will replace this stub.
- `POST /sites/onboard` (PRD §12 step 3) — validates a site URL +
  application password submission against the live site and persists the
  onboarded site record on success.

Boundary discipline: intents and results are validated against the
shared contract on every path.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from shared_contract import (
    CONTRACT_VERSION,
    ContractValidationError,
    validate_intent,
    validate_result,
)

from .config import Settings
from .onboarding import OnboardedSiteStore, OnboardResult, onboard_site


def _stub_change_id() -> str:
    return f"stub-{uuid.uuid4().hex[:12]}"


class OnboardRequest(BaseModel):
    site_url: str
    username: str
    app_password: str
    owner_id: str


def create_app(
    settings: Settings | None = None,
    onboarding_runner: Callable | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="WP-Bot Track B (WordPress site/state)",
        version="0.1.0",
        description=(
            "Track B service: WordPress site onboarding (PRD §12) and, once the "
            "stub is replaced, intent application through the B1 client."
        ),
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "track-b"}

    @app.post("/sites/onboard")
    async def onboard(payload: OnboardRequest) -> dict[str, Any]:
        """Validate site credentials and persist the site on success.

        Unambiguous result for Track A's onboarding conversation:
        success -> site_id; failure -> HTTP 422 with a specific `reason`
        (invalid_url | unreachable | not_wordpress | invalid_credentials |
        insufficient_permissions).
        """
        runner = onboarding_runner
        if runner is None:
            # Lazy: only build the store when onboarding is actually used.
            store = getattr(app.state, "onboard_store", None)
            if store is None:
                store = OnboardedSiteStore(settings.db_path)
                app.state.onboard_store = store

            async def runner(site_url, username, app_password, owner_id) -> OnboardResult:
                return await onboard_site(
                    site_url, username, app_password, owner_id, store=store
                )

        result = await runner(
            payload.site_url, payload.username, payload.app_password, payload.owner_id
        )
        if result.status == "success":
            return {
                "status": "success",
                "reason": result.reason,
                "site_id": result.site_id,
                "site_url": result.site_url,
            }
        raise HTTPException(
            status_code=422,
            detail={
                "status": "failed",
                "reason": result.reason,
                "message": result.message,
            },
        )

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
