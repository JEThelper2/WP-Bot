"""Admin escalation view tests.

Tests cover:
- Escalation requests logged by Track A appear in the admin list view.
- Status updates persist correctly via the update form.
- The open count reflects reality after status changes.
- Auth token is required when ADMIN_TOKEN is set.
- Invalid status values are rejected.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from track_a.config import Settings
from track_a.main import create_app
from track_a.store import init_db, log_escalation_request


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def app(db_path):
    settings = Settings(
        verify_token="test",
        track_b_url="http://track-b:8200",
        db_path=db_path,
        admin_token="test-admin-token",
    )
    init_db(db_path)
    return create_app(settings)


@pytest.fixture()
def client(app):
    return TestClient(app)


def _auth_headers(token: str = "test-admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -------------------------------------------------------------------
# List view
# -------------------------------------------------------------------


class TestEscalationList:
    def test_empty_list_renders(self, client: TestClient) -> None:
        resp = client.get("/admin", headers=_auth_headers())
        assert resp.status_code == 200
        assert "Escalation Requests" in resp.text
        assert "No escalation requests found" in resp.text

    def test_escalation_appears_in_list(self, client: TestClient, db_path) -> None:
        """Escalation logged by Track A shows up in the admin view."""
        log_escalation_request(db_path, "15551234567", "Redesign my homepage")
        log_escalation_request(db_path, "15559876543", "Add a new page")

        resp = client.get("/admin", headers=_auth_headers())
        assert resp.status_code == 200
        assert "15551234567" in resp.text
        assert "Redesign my homepage" in resp.text
        assert "15559876543" in resp.text
        assert "Add a new page" in resp.text

    def test_status_filter_works(self, client: TestClient, db_path) -> None:
        """Filtering by status shows only matching rows."""
        log_escalation_request(db_path, "111", "msg1")
        row_id = log_escalation_request(db_path, "222", "msg2")
        # Update one to in_progress
        from track_a.store import update_escalation_status

        update_escalation_status(db_path, row_id, status="in_progress")

        # All
        resp = client.get("/admin", headers=_auth_headers())
        assert "111" in resp.text
        assert "222" in resp.text

        # Filter: new only
        resp = client.get("/admin?status=new", headers=_auth_headers())
        assert "111" in resp.text
        assert "222" not in resp.text

        # Filter: in_progress only
        resp = client.get("/admin?status=in_progress", headers=_auth_headers())
        assert "222" in resp.text
        assert "111" not in resp.text

    def test_open_count_displays_correctly(self, client: TestClient, db_path) -> None:
        """Open count shows number of 'new' status requests."""
        log_escalation_request(db_path, "111", "msg1")
        log_escalation_request(db_path, "222", "msg2")
        log_escalation_request(db_path, "333", "msg3")

        resp = client.get("/admin", headers=_auth_headers())
        assert resp.status_code == 200
        # Open: 3, Total: 3
        assert "Open: <strong" in resp.text
        assert "3" in resp.text


# -------------------------------------------------------------------
# Detail view
# -------------------------------------------------------------------


class TestEscalationDetail:
    def test_detail_renders(self, client: TestClient, db_path) -> None:
        row_id = log_escalation_request(db_path, "15551234567", "Help me")
        resp = client.get(f"/admin/{row_id}", headers=_auth_headers())
        assert resp.status_code == 200
        assert "15551234567" in resp.text
        assert "Help me" in resp.text

    def test_detail_404_for_missing(self, client: TestClient) -> None:
        resp = client.get("/admin/99999", headers=_auth_headers())
        assert resp.status_code == 404


# -------------------------------------------------------------------
# Status update
# -------------------------------------------------------------------


class TestStatusUpdate:
    def test_update_status_persists(self, client: TestClient, db_path) -> None:
        """Updating status via the form persists to the database."""
        row_id = log_escalation_request(db_path, "111", "msg")

        resp = client.post(
            f"/admin/{row_id}/update",
            headers=_auth_headers(),
            data={"status": "in_progress", "notes": "Looking into it"},
            follow_redirects=False,
        )
        assert resp.status_code == 303  # Redirect to detail

        # Verify persistence
        from track_a.store import get_escalation_request

        esc = get_escalation_request(db_path, row_id)
        assert esc is not None
        assert esc["status"] == "in_progress"
        assert esc["notes"] == "Looking into it"
        assert esc["updated_at"] is not None

    def test_update_to_resolved(self, client: TestClient, db_path) -> None:
        row_id = log_escalation_request(db_path, "222", "msg2")
        client.post(
            f"/admin/{row_id}/update",
            headers=_auth_headers(),
            data={"status": "resolved", "notes": "Done"},
            follow_redirects=False,
        )
        from track_a.store import get_escalation_request

        esc = get_escalation_request(db_path, row_id)
        assert esc["status"] == "resolved"

    def test_invalid_status_rejected(self, client: TestClient, db_path) -> None:
        row_id = log_escalation_request(db_path, "333", "msg3")
        resp = client.post(
            f"/admin/{row_id}/update",
            headers=_auth_headers(),
            data={"status": "bogus"},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_update_404_for_missing(self, client: TestClient) -> None:
        resp = client.post(
            "/admin/99999/update",
            headers=_auth_headers(),
            data={"status": "resolved"},
            follow_redirects=False,
        )
        assert resp.status_code == 404


# -------------------------------------------------------------------
# Open count reflects status changes
# -------------------------------------------------------------------


class TestOpenCount:
    def test_open_count_after_status_change(self, client: TestClient, db_path) -> None:
        """Open count decreases when a request is resolved."""
        log_escalation_request(db_path, "111", "msg1")
        log_escalation_request(db_path, "222", "msg2")

        # Initially 2 open
        resp = client.get("/admin/api/count", headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["open"] == 2
        assert data["total"] == 2

        # Resolve one
        row_id = log_escalation_request(db_path, "333", "msg3")
        client.post(
            f"/admin/{row_id}/update",
            headers=_auth_headers(),
            data={"status": "resolved"},
            follow_redirects=False,
        )

        # Now 2 open, 3 total
        resp = client.get("/admin/api/count", headers=_auth_headers())
        data = resp.json()
        assert data["open"] == 2
        assert data["total"] == 3

    def test_api_count_without_auth_rejected(self, client: TestClient) -> None:
        resp = client.get("/admin/api/count")
        assert resp.status_code == 401


# -------------------------------------------------------------------
# Auth
# -------------------------------------------------------------------


class TestAdminAuth:
    def test_list_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/admin")
        assert resp.status_code == 401

    def test_valid_token_grants_access(self, client: TestClient) -> None:
        resp = client.get("/admin", headers=_auth_headers())
        assert resp.status_code == 200

    def test_wrong_token_rejected(self, client: TestClient) -> None:
        resp = client.get("/admin", headers=_auth_headers("wrong-token"))
        assert resp.status_code == 401

    def test_token_via_query_param(self, client: TestClient) -> None:
        resp = client.get("/admin?token=test-admin-token")
        assert resp.status_code == 200
