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
from track_b.stub import create_stub_app as create_track_b_app


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


def _contract_result(status: str = "success", **overrides) -> dict:
    r = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "change_id": "ch-x",
        "before": None,
        "after": None,
        "live_url": None,
        "error_message": None,
    }
    r.update(overrides)
    return r


def test_submit_intent_with_decision_appends_the_query_param() -> None:
    """YES/NO resolutions ride on ?decision=... (the body stays a pure
    intent object, since the schema forbids extra keys)."""
    from fastapi import FastAPI, Request

    seen: list[str] = []

    fake_b = FastAPI()

    @fake_b.post("/intent")
    async def intent(request: Request) -> dict:
        seen.append(str(request.url))
        return _contract_result("needs_confirmation", change_id="pc-1")

    transport = httpx.ASGITransport(app=fake_b)
    http = httpx.AsyncClient(transport=transport, base_url="http://track-b")
    client = TrackBClient(base_url="http://track-b", client=http)

    run(client.submit_intent(valid_intent()))
    run(client.submit_intent(valid_intent(), decision="yes"))
    run(client.submit_intent(valid_intent(), decision="no"))

    assert seen == [
        "http://track-b/intent",
        "http://track-b/intent?decision=yes",
        "http://track-b/intent?decision=no",
    ]


def test_undo_posts_owner_id_and_returns_validated_result() -> None:
    from fastapi import FastAPI, Request

    seen: dict = {}

    fake_b = FastAPI()

    @fake_b.post("/undo")
    async def undo(request: Request) -> dict:
        seen["body"] = await request.json()
        return _contract_result(live_url="https://wp.example.com/?p=1")

    transport = httpx.ASGITransport(app=fake_b)
    http = httpx.AsyncClient(transport=transport, base_url="http://track-b")
    client = TrackBClient(base_url="http://track-b", client=http)

    result = run(client.undo("owner-1"))
    assert seen["body"] == {"owner_id": "owner-1"}
    assert result["status"] == "success"
    assert result["live_url"] == "https://wp.example.com/?p=1"


def test_undo_non_contract_result_raises() -> None:
    from fastapi import FastAPI

    fake_b = FastAPI()

    @fake_b.post("/undo")
    async def garbage(payload: dict) -> dict:
        return {"status": "success"}  # missing change_id / contract_version

    transport = httpx.ASGITransport(app=fake_b)
    http = httpx.AsyncClient(transport=transport, base_url="http://track-b")
    client = TrackBClient(base_url="http://track-b", client=http)

    with pytest.raises(TrackBError):
        run(client.undo("owner-1"))
