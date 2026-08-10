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

    async def submit_intent(self, intent: dict[str, Any]) -> dict[str, Any]:
        """Send an intent object to Track B and return its result object."""
        # Outbound boundary: never send a non-contract intent.
        validate_intent(intent)

        resp = await self._client.post(
            f"{self.base_url}/intent", json=intent, timeout=30.0
        )

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
