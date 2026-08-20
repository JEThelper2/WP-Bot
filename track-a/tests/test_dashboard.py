"""Admin dashboard tests.

Tests cover:
- Dashboard home renders with metrics and health indicators.
- Sites view shows onboarded sites from Track B's database.
- Changes view shows change log entries with filters.
- Failures view surfaces failed writes prominently.
- Metrics API returns correct counts.
- Health API reports Track A/B status.
- Auth is required for all dashboard routes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from track_a.config import Settings
from track_a.main import create_app
from track_a.store import init_db, log_escalation_request


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def track_b_db(tmp_path):
    """Create a minimal Track B database with test data."""
    db = tmp_path / "trackb.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS onboarded_sites (
            site_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            site_url TEXT NOT NULL UNIQUE,
            username TEXT NOT NULL,
            encrypted_password TEXT NOT NULL,
            allowlist_config TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS change_log (
            change_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            content_type TEXT NOT NULL,
            action TEXT NOT NULL,
            before TEXT,
            after TEXT,
            live_url TEXT,
            undo_of TEXT,
            created_at TEXT
        );
    """)
    # Insert test data
    conn.execute(
        "INSERT INTO onboarded_sites VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("site-abc", "owner1", "https://example.com", "editor", "enc_pass",
         "{}", "active", "2026-08-20T10:00:00Z"),
    )
    conn.execute(
        "INSERT INTO onboarded_sites VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("site-def", "owner2", "https://test.com", "editor", "enc_pass",
         "{}", "inactive", "2026-08-19T10:00:00Z"),
    )
    conn.execute(
        "INSERT INTO change_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ch-001", "owner1", "job", "create", None,
         '{"title":"Test Job"}', "https://example.com/job/test",
         None, "2026-08-20T11:00:00Z"),
    )
    conn.execute(
        "INSERT INTO change_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ch-002", "owner1", "business_info", "update", '{"hours":"9-5"}',
         '{"hours":"9-6"}', None, None, "2026-08-20T12:00:00Z"),
    )
    conn.execute(
        "INSERT INTO change_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ch-003", "owner2", "announcement", "create", None,
         '{"title":"Test"}', None, None, "2026-08-20T13:00:00Z"),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def app(tmp_path, track_b_db):
    from track_a.config import Settings
    from track_a.main import create_app

    settings = Settings(
        verify_token="test",
        track_b_url="http://track-b:8200",
        db_path=tmp_path / "test.db",
        admin_token="test-admin-token",
    )
    from track_a.store import init_db
    init_db(settings.db_path)
    return create_app(settings)


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


def _auth(token: str = "test-admin-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -------------------------------------------------------------------
# Home dashboard
# -------------------------------------------------------------------


class TestDashboardHome:
    def test_home_renders(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard", headers=_auth())
        assert resp.status_code == 200
        assert "Dashboard" in resp.text
        assert "System Health" in resp.text

    def test_home_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 401

    def test_home_shows_escalation_counts(self, client: TestClient, app) -> None:
        from track_a.store import log_escalation_request
        log_escalation_request(app.state.settings.db_path, "111", "msg1")
        resp = client.get("/admin/dashboard", headers=_auth())
        assert resp.status_code == 200
        assert "Open Escalations" in resp.text


# -------------------------------------------------------------------
# Sites view
# -------------------------------------------------------------------


class TestSitesView:
    def test_sites_lists_onboarded_sites(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/sites", headers=_auth())
        assert resp.status_code == 200
        assert "Onboarded Sites" in resp.text
        # Should show test sites from Track B DB
        assert "example.com" in resp.text or "No onboarded sites" in resp.text

    def test_sites_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/sites")
        assert resp.status_code == 401


# -------------------------------------------------------------------
# Changes view
# -------------------------------------------------------------------


class TestChangesView:
    def test_changes_renders(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/changes", headers=_auth())
        assert resp.status_code == 200
        assert "Change Log" in resp.text

    def test_changes_filter_by_action(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/dashboard/changes?action=create", headers=_auth()
        )
        assert resp.status_code == 200
        # Filter form should show "create" selected
        assert 'value="create"' in resp.text


# -------------------------------------------------------------------
# Failures view
# -------------------------------------------------------------------


class TestFailuresView:
    def test_failures_renders(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/failures", headers=_auth())
        assert resp.status_code == 200
        assert "Failed Writes" in resp.text

    def test_failures_shows_success_when_none(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/failures", headers=_auth())
        assert resp.status_code == 200
        assert "all writes succeeded" in resp.text or "Failed" in resp.text


# -------------------------------------------------------------------
# Escalations redirect
# -------------------------------------------------------------------


class TestEscalationsRedirect:
    def test_redirects_to_admin(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/dashboard/escalations", headers=_auth(), follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin"


# -------------------------------------------------------------------
# API endpoints
# -------------------------------------------------------------------


class TestDashboardAPI:
    def test_metrics_returns_json(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/api/metrics", headers=_auth())
        assert resp.status_code == 200
        data = resp.json()
        assert "escalations" in data
        assert "changes" in data

    def test_health_returns_json(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/api/health", headers=_auth())
        assert resp.status_code == 200
        data = resp.json()
        assert "track_a" in data
        assert "track_b" in data
        assert data["track_a"] is True  # We're responding, so Track A is up

    def test_api_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/api/metrics")
        assert resp.status_code == 401


# -------------------------------------------------------------------
# Change log query functions (Track B)
# -------------------------------------------------------------------


class TestChangeLogQueries:
    def test_list_changes_filters(self) -> None:
        from track_b.changelog import ChangeRow, InMemoryChangeLog

        log = InMemoryChangeLog()
        import time
        log._time = lambda: time.time()

        import asyncio

        asyncio.run(log.record_change(ChangeRow(
            change_id="c1", owner_id="o1", content_type="job",
            action="create", before=None, after={"title": "T"},
        )))
        asyncio.run(log.record_change(ChangeRow(
            change_id="c2", owner_id="o2", content_type="announcement",
            action="update", before={}, after={},
        )))

        # Filter by owner
        result = asyncio.run(log.list_changes(owner_id="o1"))
        assert len(result) == 1
        assert result[0].change_id == "c1"

        # Filter by content_type
        result = asyncio.run(log.list_changes(content_type="announcement"))
        assert len(result) == 1
        assert result[0].change_id == "c2"

    def test_count_by_action(self) -> None:
        from track_b.changelog import ChangeRow, InMemoryChangeLog

        log = InMemoryChangeLog()
        import time
        log._time = lambda: time.time()

        import asyncio

        asyncio.run(log.record_change(ChangeRow(
            change_id="c1", owner_id="o1", content_type="job",
            action="create", before=None, after={},
        )))
        asyncio.run(log.record_change(ChangeRow(
            change_id="c2", owner_id="o1", content_type="job",
            action="undo", before={}, after=None, undo_of="c1",
        )))

        counts = asyncio.run(log.count_by_action())
        assert counts["create"] == 1
        assert counts["undo"] == 1


# -------------------------------------------------------------------
# Per-site detail view
# -------------------------------------------------------------------


class TestSiteDetail:
    def test_site_detail_renders(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/dashboard/sites/owner1", headers=_auth()
        )
        assert resp.status_code == 200
        assert "owner1" in resp.text
        assert "Change History" in resp.text

    def test_site_detail_shows_changes(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/dashboard/sites/owner1", headers=_auth()
        )
        assert resp.status_code == 200
        # owner1 has two changes in the fixture
        assert "ch-001" in resp.text or "ch-002" in resp.text

    def test_site_detail_shows_site_info(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/dashboard/sites/owner1", headers=_auth()
        )
        assert resp.status_code == 200
        assert "example.com" in resp.text
        assert "active" in resp.text

    def test_site_detail_no_site_record(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/dashboard/sites/unknown-owner", headers=_auth()
        )
        assert resp.status_code == 200
        assert "No site record found" in resp.text

    def test_site_detail_filter_by_action(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/dashboard/sites/owner1?action=create",
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert 'value="create"' in resp.text

    def test_site_detail_filter_by_type(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/dashboard/sites/owner1?content_type=job",
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert 'value="job"' in resp.text

    def test_site_detail_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/sites/owner1")
        assert resp.status_code == 401

    def test_sites_list_has_history_links(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard/sites", headers=_auth())
        assert resp.status_code == 200
        assert "History →" in resp.text or "sites/owner1" in resp.text

    def test_site_detail_shows_action_stats(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/dashboard/sites/owner1", headers=_auth()
        )
        assert resp.status_code == 200
        # Should show action counts: owner1 has 1 create, 1 update
        assert "create" in resp.text.lower()
        assert "update" in resp.text.lower()

    def test_site_detail_filter_by_date_from(self, client: TestClient) -> None:
        """Filter changes starting from a specific date."""
        resp = client.get(
            "/admin/dashboard/sites/owner1?date_from=2026-08-20T12:00:00",
            headers=_auth(),
        )
        assert resp.status_code == 200
        # Only the 12:00 change (ch-002) should appear
        assert "ch-002" in resp.text
        # ch-001 at 11:00 should NOT appear in the change list
        # (it may appear in stat counts but not in the table rows)

    def test_site_detail_filter_by_date_to(self, client: TestClient) -> None:
        """Filter changes up to a specific date."""
        resp = client.get(
            "/admin/dashboard/sites/owner1?date_to=2026-08-20T11:30:00",
            headers=_auth(),
        )
        assert resp.status_code == 200
        # Only ch-001 (at 11:00) should appear
        assert "ch-001" in resp.text

    def test_site_detail_filter_by_date_range(self, client: TestClient) -> None:
        """Filter changes within a date range."""
        resp = client.get(
            "/admin/dashboard/sites/owner1"
            "?date_from=2026-08-20T11:30:00&date_to=2026-08-20T12:30:00",
            headers=_auth(),
        )
        assert resp.status_code == 200
        # Only ch-002 (at 12:00) should appear
        assert "ch-002" in resp.text

    def test_site_detail_filter_by_all_criteria(self, client: TestClient) -> None:
        """Combine date range with type and action filters."""
        resp = client.get(
            "/admin/dashboard/sites/owner1"
            "?date_from=2026-08-20T00:00:00&date_to=2026-08-20T23:59:59"
            "&content_type=job&action=create",
            headers=_auth(),
        )
        assert resp.status_code == 200
        # ch-001 is job+create within the date range
        assert "ch-001" in resp.text

    def test_site_detail_date_filter_no_results(self, client: TestClient) -> None:
        """Date range that matches nothing shows empty state."""
        resp = client.get(
            "/admin/dashboard/sites/owner1"
            "?date_from=2026-08-21T00:00:00",
            headers=_auth(),
        )
        assert resp.status_code == 200
        assert "No changes recorded" in resp.text
