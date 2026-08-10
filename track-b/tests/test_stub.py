"""Tests for the Track B stub endpoint."""

import pytest
from fastapi.testclient import TestClient
from shared_contract import CONTRACT_VERSION, validate_result

from track_b.main import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


VALID_INTENT = {
    "contract_version": CONTRACT_VERSION,
    "owner_id": "owner-1",
    "action": "update",
    "content_type": "business_info",
    "fields": {"hours": "Mon-Fri 9-6"},
    "confidence": 0.95,
}


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_valid_intent_returns_canned_success(client: TestClient) -> None:
    resp = client.post("/intent", json=VALID_INTENT)
    assert resp.status_code == 200
    result = resp.json()
    # The stub's output must satisfy the real result contract.
    validate_result(result)
    assert result["status"] == "success"
    assert result["change_id"].startswith("stub-")
    assert result["after"] == VALID_INTENT["fields"]
    assert result["error_message"] is None


def test_invalid_intent_rejected_with_contract_valid_failed_result(
    client: TestClient,
) -> None:
    bad = dict(VALID_INTENT)
    bad["action"] = "publish"  # not in the enum
    resp = client.post("/intent", json=bad)
    assert resp.status_code == 422
    result = resp.json()
    # Even the failure path must be a contract-valid result object.
    validate_result(result)
    assert result["status"] == "failed"
    assert "intent rejected at boundary" in result["error_message"]
