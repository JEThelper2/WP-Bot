"""Track A -> Track B client, exercised end-to-end against the stub.

Proves Track A speaks the *real* contract shapes even when talking to the
canned stub: the intent is validated outbound (validate_intent) and the
result is validated inbound (validate_result).

The client is async (it will be awaited from FastAPI handlers), so these
sync tests drive it with asyncio.run().
"""

import asyncio

import httpx
import pytest
from shared_contract import CONTRACT_VERSION, ContractValidationError

from track_a.trackb import TrackBClient, TrackBError
from track_b.main import create_app as create_track_b_app


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def track_b_client() -> TrackBClient:
    transport = httpx.ASGITransport(app=create_track_b_app())
    http = httpx.AsyncClient(transport=transport, base_url="http://track-b")
    return TrackBClient(base_url="http://track-b", client=http)


def valid_intent() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "owner_id": "owner-1",
        "action": "update",
        "content_type": "business_info",
        "fields": {"hours": "Mon-Fri 9-6"},
        "confidence": 0.95,
    }


def test_submit_intent_returns_contract_valid_success(track_b_client: TrackBClient) -> None:
    result = run(track_b_client.submit_intent(valid_intent()))
    assert result["status"] == "success"
    assert result["change_id"].startswith("stub-")
    assert result["after"] == {"hours": "Mon-Fri 9-6"}
    assert result["error_message"] is None


def test_invalid_intent_is_rejected_before_any_http(track_b_client: TrackBClient) -> None:
    bad = valid_intent()
    bad["confidence"] = 2.0  # out of range
    with pytest.raises(ContractValidationError):
        run(track_b_client.submit_intent(bad))


def test_failed_result_from_track_b_is_returned_and_validated() -> None:
    # A future Track B will reject intents for business reasons (e.g. content
    # not found) with a contract-valid failed result. Simulate that with a
    # minimal fake Track B so we exercise the client's inbound handling.
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    fake_b = FastAPI()

    @fake_b.post("/intent")
    async def reject(payload: dict) -> JSONResponse:
        return JSONResponse(
            {
                "contract_version": CONTRACT_VERSION,
                "status": "failed",
                "change_id": "fake-1",
                "before": None,
                "after": None,
                "live_url": None,
                "error_message": "no such content on the site",
            },
            status_code=422,
        )

    transport = httpx.ASGITransport(app=fake_b)
    http = httpx.AsyncClient(transport=transport, base_url="http://track-b")
    client = TrackBClient(base_url="http://track-b", client=http)

    result = run(client.submit_intent(valid_intent()))
    assert result["status"] == "failed"
    assert result["error_message"] == "no such content on the site"


def test_non_contract_result_from_track_b_raises() -> None:
    # A broken Track B that returns garbage must be caught at the boundary.
    from fastapi import FastAPI

    fake_b = FastAPI()

    @fake_b.post("/intent")
    async def garbage(payload: dict) -> dict:
        return {"status": "success"}  # missing change_id / contract_version

    transport = httpx.ASGITransport(app=fake_b)
    http = httpx.AsyncClient(transport=transport, base_url="http://track-b")
    client = TrackBClient(base_url="http://track-b", client=http)

    with pytest.raises(TrackBError):
        run(client.submit_intent(valid_intent()))
