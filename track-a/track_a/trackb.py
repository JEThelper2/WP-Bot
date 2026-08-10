"""Client Track A uses to call Track B.

This is where the two services meet, so both directions go through the
shared contract (never trust the other side):

- outbound: the intent is validated with `validate_intent` before it is
  sent, so Track A never ships a non-contract intent;
- inbound: the result is validated with `validate_result` before it is
  returned, so a misbehaving (or buggy) Track B can never smuggle a
  non-contract result into Track A.

`submit_intent` returns the result object as-is; callers inspect
`result["status"]` ("success" | "failed" | "needs_confirmation").

Against the real Track B API (B6):

- `submit_intent(intent)` with no decision **stages** the intent as
  pending and returns `needs_confirmation` with a change_id;
- `submit_intent(intent, decision="yes")` **resolves** the staged
  confirmation and runs the write pipeline;
- `submit_intent(intent, decision="no")` discards the staged
  confirmation without writing;
- `undo(owner_id)` reverse-applies the owner's most recent change (B4).
"""

from __future__ import annotations

from typing import Any

import httpx
from shared_contract import (
    ContractValidationError,
    validate_intent,
    validate_result,
)


class TrackBError(Exception):
    """Track B responded in a way that violates the shared contract."""


class TrackBClient:
    def __init__(
        self, base_url: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient()

    async def submit_intent(
        self, intent: dict[str, Any], *, decision: str | None = None
    ) -> dict[str, Any]:
        """Send an intent object to Track B and return its result object.

        `decision=None` stages the intent as pending (Track B answers
        `needs_confirmation` with a change_id). `decision="yes"` resolves
        the pending confirmation and runs the write pipeline;
        `decision="no"` discards it without writing.
        """
        # Outbound boundary: never send a non-contract intent.
        validate_intent(intent)

        url = f"{self.base_url}/intent"
        if decision is not None:
            url += f"?decision={decision}"
        resp = await self._client.post(url, json=intent, timeout=30.0)

        # Track B signals a failed result with HTTP 422; a contract-valid
        # result body should arrive either way. Anything else is a transport
        # error.
        if resp.status_code not in (200, 422):
            resp.raise_for_status()

        result = resp.json()
        # Inbound boundary: never trust Track B.
        try:
            validate_result(result)
        except ContractValidationError as exc:
            raise TrackBError(
                f"Track B returned a result that fails the contract: {exc}"
            ) from exc
        return result

    async def undo(self, owner_id: str) -> dict[str, Any]:
        """Ask Track B to reverse the owner's most recent change (B4).

        Same boundary discipline as submit_intent: the result is validated
        against the contract before it is trusted.
        """
        resp = await self._client.post(
            f"{self.base_url}/undo", json={"owner_id": owner_id}, timeout=30.0
        )
        if resp.status_code not in (200, 422):
            resp.raise_for_status()

        result = resp.json()
        try:
            validate_result(result)
        except ContractValidationError as exc:
            raise TrackBError(
                f"Track B returned a result that fails the contract: {exc}"
            ) from exc
        return result

    async def onboard_site(
        self,
        *,
        site_url: str,
        username: str,
        app_password: str,
        owner_id: str,
    ) -> dict[str, Any]:
        """Submit a site for onboarding validation (PRD §12 / B5).

        The onboarding endpoint is not a contract result endpoint, so the
        response is normalized here instead: on success Track B answers
        HTTP 200 with `{status, reason, site_id, site_url}`; on failure it
        answers HTTP 422 with the specific `reason` in the detail body.
        Either way the caller gets a single dict with `status`
        ("success" | "failed"), `reason`, `message`, and `site_id`.
        """
        resp = await self._client.post(
            f"{self.base_url}/sites/onboard",
            json={
                "site_url": site_url,
                "username": username,
                "app_password": app_password,
                "owner_id": owner_id,
            },
            timeout=30.0,
        )
        if resp.status_code == 200:
            body = resp.json()
            return {
                "status": "success",
                "reason": body.get("reason"),
                "message": body.get("message", ""),
                "site_id": body.get("site_id"),
                "site_url": body.get("site_url"),
            }
        if resp.status_code == 422:
            detail = resp.json().get("detail", {}) or {}
            return {
                "status": "failed",
                "reason": detail.get("reason"),
                "message": detail.get("message", ""),
                "site_id": None,
                "site_url": None,
            }
        resp.raise_for_status()
