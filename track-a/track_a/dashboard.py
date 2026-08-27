"""Internal admin dashboard for system monitoring.

Builds on the same admin auth from Stage 3 (ADMIN_TOKEN).  Provides
operational visibility into every pilot site's activity, errors, and
escalations without needing to query the database directly.

Enhancements (Stage 4):
- Date range filtering on changes, failures, and metrics
- Auto-refresh via AJAX polling (every 30s)
- Per-site activity summary with last activity timestamp
- Undo count and content type breakdown in metrics
- Simple CSS bar chart for activity visualization
- CSV export for change log
- Search by change_id
- Responsive design with hover states

Routes (all under /admin/dashboard):
    GET  /admin/dashboard               — home: summary metrics + health
    GET  /admin/dashboard/sites         — onboarded sites list with activity
    GET  /admin/dashboard/changes       — change log with filters + date range
    GET  /admin/dashboard/failures      — recent failed writes (prominent)
    GET  /admin/dashboard/escalations   — redirect to /admin (Stage 3 view)
    GET  /admin/dashboard/api/metrics   — JSON metrics for dashboards
    GET  /admin/dashboard/api/health    — JSON health status
    GET  /admin/dashboard/api/refresh   — AJAX endpoint for live updates
    GET  /admin/dashboard/api/activity  — JSON: recent changes for live feed
    GET  /admin/dashboard/changes.csv   — CSV export of change log
"""

from __future__ import annotations

import csv
import hmac
import html
import io
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .store import count_escalation_requests, count_open_escalations

logger = logging.getLogger("track_a.dashboard")

router = APIRouter(prefix="/admin/dashboard", tags=["dashboard"])

TRACK_B_URL = os.environ.get("TRACK_B_URL", "http://127.0.0.1:8200")

# Auto-refresh interval in seconds
AUTO_REFRESH_SECONDS = 30


def _check_auth(request: Request) -> None:
    """Verify admin token — same logic as admin.py."""
    token = getattr(request.app.state, "admin_token", None) or os.environ.get("ADMIN_TOKEN", "")
    if not token:
        return
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        provided = auth_header[7:]
    else:
        provided = request.query_params.get("token", "")
    if not hmac.compare_digest(provided, token):
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Unauthorized")


def _escap(s: str | None) -> str:
    return html.escape(str(s) if s is not None else "")


def _status_color(status: str) -> str:
    return {
        "active": "#27ae60",
        "inactive": "#e74c3c",
        "new": "#e74c3c",
        "in_progress": "#f39c12",
        "resolved": "#27ae60",
        "create": "#3498db",
        "update": "#f39c12",
        "delete": "#e74c3c",
        "undo": "#9b59b6",
        "failed": "#e74c3c",
    }.get(status, "#95a5a6")


def _badge(text: str, color: str | None = None) -> str:
    c = color or _status_color(text)
    return f'<span class="badge" style="background:{c};">{_escap(text)}</span>'


def _parse_date(d: str | None) -> str | None:
    """Normalize a date string to ISO format for SQL comparison."""
    if not d:
        return None
    try:
        # Accept YYYY-MM-DD or full ISO
        if len(d) == 10:
            return d + "T00:00:00"
        return d
    except Exception:
        return None


# -----------------------------------------------------------------------
# Shared CSS (extracted to avoid repetition)
# -----------------------------------------------------------------------

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
h1 { color: #2c3e50; margin-bottom: 8px; }
h2 { color: #34495e; margin-top: 24px; }
.badge { color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; white-space: nowrap; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
.card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); transition: box-shadow 0.2s; }
.card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
.card-num { font-size: 2em; font-weight: bold; line-height: 1.2; }
.card-label { color: #888; font-size: 0.9em; margin-top: 4px; }
.health { display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
.health-ok { background: #27ae60; }
.health-fail { background: #e74c3c; }
.health-unknown { background: #95a5a6; }
nav { margin-bottom: 20px; display: flex; gap: 16px; flex-wrap: wrap; }
nav a { color: #3498db; text-decoration: none; font-weight: 500; padding: 4px 0; }
nav a:hover { text-decoration: underline; }
nav a.active { color: #2c3e50; font-weight: 700; border-bottom: 2px solid #2c3e50; }
table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; }
th { background: #2c3e50; color: white; font-weight: 600; }
tr:hover { background: #f8f9fa; }
.filters { background: white; padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: flex; gap: 12px; align-items: end; flex-wrap: wrap; }
.filters label { font-size: 0.9em; color: #555; }
.filters select, .filters input { padding: 6px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
.filters button { background: #3498db; color: white; border: none; padding: 6px 16px; border-radius: 4px; cursor: pointer; font-weight: 500; }
.filters button:hover { background: #2980b9; }
.filters button.secondary { background: #95a5a6; }
.filters button.secondary:hover { background: #7f8c8d; }
a { color: #3498db; text-decoration: none; }
a:hover { text-decoration: underline; }
.refresh-indicator { position: fixed; top: 10px; right: 10px; background: #27ae60; color: white; padding: 4px 12px; border-radius: 4px; font-size: 0.8em; opacity: 0; transition: opacity 0.3s; }
.refresh-indicator.show { opacity: 1; }
.chart-bar { display: inline-block; height: 20px; border-radius: 3px; vertical-align: middle; min-width: 2px; transition: width 0.3s; }
.empty-state { text-align: center; color: #888; padding: 40px; font-size: 1.1em; }
.timestamp { color: #888; font-size: 0.85em; }
@media (max-width: 768px) {
    .grid { grid-template-columns: repeat(2, 1fr); }
    .filters { flex-direction: column; }
    table { font-size: 0.9em; }
}
"""

_JS_AUTO_REFRESH = f"""
<script>
// Auto-refresh every {AUTO_REFRESH_SECONDS} seconds
let refreshInterval = setInterval(refreshData, {AUTO_REFRESH_SECONDS * 1000});
let lastRefresh = new Date();
let lastActivityTime = '';
const actionColors = {{create:'#3498db', update:'#f39c12', delete:'#e74c3c', undo:'#9b59b6', failed:'#e74c3c'}};

async function refreshData() {{
    try {{
        const resp = await fetch('/admin/dashboard/api/refresh?' + Date.now());
        if (resp.ok) {{
            const data = await resp.json();
            updateMetrics(data);
            showRefreshIndicator();
        }}
    }} catch(e) {{
        console.error('Refresh failed:', e);
    }}
    // Also refresh activity feed
    refreshActivity();
}}

async function refreshActivity() {{
    const feed = document.getElementById('activity-feed');
    if (!feed) return;
    try {{
        let url = '/admin/dashboard/api/activity?limit=15';
        if (lastActivityTime) url += '&since=' + encodeURIComponent(lastActivityTime);
        const resp = await fetch(url + '&' + Date.now());
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.changes && data.changes.length > 0) {{
            renderActivity(data.changes, !lastActivityTime);
            lastActivityTime = data.changes[0].created_at;
        }} else if (!lastActivityTime) {{
            feed.innerHTML = '<div style="padding:16px;color:#888;text-align:center;">No recent activity</div>';
        }}
    }} catch(e) {{
        console.error('Activity refresh failed:', e);
    }}
}}

function renderActivity(changes, replace) {{
    const feed = document.getElementById('activity-feed');
    if (!feed) return;
    const html = changes.map(c => {{
        const color = actionColors[c.action] || '#95a5a6';
        const time = c.created_at ? c.created_at.substring(11, 19) : '';
        const date = c.created_at ? c.created_at.substring(0, 10) : '';
        return `<div style="padding:10px 16px;border-bottom:1px solid #eee;display:flex;align-items:center;gap:10px;">
            <span style="background:${{color}};color:white;padding:2px 8px;border-radius:4px;font-size:0.8em;min-width:60px;text-align:center;">${{c.action}}</span>
            <span style="background:#ecf0f1;padding:2px 6px;border-radius:3px;font-size:0.8em;">${{c.content_type}}</span>
            <span style="flex:1;font-size:0.9em;color:#333;">${{c.summary || c.owner_id}}</span>
            <span style="font-size:0.8em;color:#888;white-space:nowrap;">${{date}} ${{time}}</span>
        </div>`;
    }}).join('');
    if (replace) {{
        feed.innerHTML = html;
    }} else {{
        feed.innerHTML = html + feed.innerHTML;
    }}
}}

function updateMetrics(data) {{
    const cards = document.querySelectorAll('[data-metric]');
    cards.forEach(card => {{
        const key = card.getAttribute('data-metric');
        if (data[key] !== undefined) {{
            card.textContent = data[key];
        }}
    }});
}}

function showRefreshIndicator() {{
    const el = document.getElementById('refresh-indicator');
    if (el) {{
        el.classList.add('show');
        setTimeout(() => el.classList.remove('show'), 1500);
    }}
    lastRefresh = new Date();
}}

// Initial load of activity feed
refreshActivity();

// Pause refresh when tab is hidden
document.addEventListener('visibilitychange', () => {{
    if (document.hidden) {{
        clearInterval(refreshInterval);
    }} else {{
        refreshInterval = setInterval(refreshData, {AUTO_REFRESH_SECONDS * 1000});
        refreshData();
    }}
}});
</script>
"""


def _nav(active: str) -> str:
    """Render the navigation bar with the active page highlighted."""
    items = [
        ("home", "/admin/dashboard", "Home"),
        ("sites", "/admin/dashboard/sites", "Sites"),
        ("changes", "/admin/dashboard/changes", "Changes"),
        ("failures", "/admin/dashboard/failures", "Failures"),
        ("escalations", "/admin", "Escalations"),
    ]
    links = ""
    for key, href, label in items:
        cls = ' class="active"' if key == active else ""
        links += f'<a href="{href}"{cls}>{label}</a>'
    return f"<nav>{links}</nav>"


def _page(title: str, active: str, body: str, extra_head: str = "") -> str:
    """Wrap body content in a full HTML page with shared CSS."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{_escap(title)} — WP-Bot Dashboard</title>
    <style>{_CSS}</style>
    {extra_head}
</head>
<body>
    <div id="refresh-indicator" class="refresh-indicator">✓ Refreshed</div>
    <h1>{_escap(title)}</h1>
    {_nav(active)}
    {body}
    {_JS_AUTO_REFRESH}
</body>
</html>"""


def _bar_chart(counts: dict[str, int], max_width: int = 300) -> str:
    """Simple CSS bar chart for action counts."""
    if not counts:
        return ""
    total = max(counts.values()) if counts else 1
    bars = ""
    for action, count in sorted(counts.items(), key=lambda x: -x[1]):
        width = int((count / total) * max_width) if total > 0 else 0
        color = _status_color(action)
        bars += (
            f'<div style="margin-bottom:4px;display:flex;align-items:center;gap:8px;">'
            f'<span style="width:80px;text-align:right;font-size:0.9em;">{_escap(action)}</span>'
            f'<div class="chart-bar" style="background:{color};width:{width}px;"></div>'
            f'<span style="font-size:0.9em;font-weight:500;">{count}</span>'
            f"</div>"
        )
    return f'<div style="background:white;padding:16px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">{bars}</div>'


# -----------------------------------------------------------------------
# Jinja2 template engine
# -----------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_jinja_env.filters["status_color"] = _status_color
_jinja_env.filters["badge"] = lambda text, color=None: (
    f'<span class="badge" style="background:{color or _status_color(text)};">'
    f"{html.escape(str(text))}</span>"
)


def _render(template_name: str, context: dict[str, Any]) -> HTMLResponse:
    """Render a Jinja2 template and return an HTMLResponse."""
    tmpl = _jinja_env.get_template(template_name)
    return HTMLResponse(content=tmpl.render(**context))


# -----------------------------------------------------------------------
# Dashboard home
# -----------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def dashboard_home(request: Request) -> HTMLResponse:
    """Summary: metrics, health, recent failures, open escalations."""
    _check_auth(request)
    db_path: Path = request.app.state.settings.db_path

    open_esc = count_open_escalations(db_path)
    total_esc = count_escalation_requests(db_path)

    sites_count = 0
    sites_active = 0
    failures_count = 0
    action_counts: dict[str, int] = {}
    content_type_counts: dict[str, int] = {}
    undo_count = 0
    track_b_ok = False

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{TRACK_B_URL}/health")
            track_b_ok = resp.status_code == 200
    except Exception:
        pass

    track_b_db = _find_track_b_db(request)
    if track_b_db:
        try:
            import sqlite3

            conn = sqlite3.connect(str(track_b_db))
            conn.row_factory = sqlite3.Row

            row = conn.execute("SELECT COUNT(*) AS n FROM onboarded_sites").fetchone()
            sites_count = row["n"] if row else 0
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM onboarded_sites WHERE status='active'"
            ).fetchone()
            sites_active = row["n"] if row else 0

            rows = conn.execute(
                "SELECT action, COUNT(*) AS n FROM change_log GROUP BY action"
            ).fetchall()
            action_counts = {r["action"]: r["n"] for r in rows}
            undo_count = action_counts.get("undo", 0)

            rows = conn.execute(
                "SELECT content_type, COUNT(*) AS n FROM change_log GROUP BY content_type"
            ).fetchall()
            content_type_counts = {r["content_type"]: r["n"] for r in rows}

            row = conn.execute(
                "SELECT COUNT(*) AS n FROM change_log WHERE action='failed'"
            ).fetchone()
            failures_count = row["n"] if row else 0

            conn.close()
        except Exception as exc:
            logger.debug("Could not read Track B data: %s", exc)

    max_count = max(action_counts.values()) if action_counts else 1
    max_ct_count = max(content_type_counts.values()) if content_type_counts else 1
    return _render("dashboard/home.html", {
        "title": "Dashboard",
        "active_page": "home",
        "track_b_ok": track_b_ok,
        "sites_active": sites_active,
        "sites_count": sites_count,
        "open_esc": open_esc,
        "total_esc": total_esc,
        "failures_count": failures_count,
        "undo_count": undo_count,
        "action_counts": action_counts,
        "content_type_counts": content_type_counts,
        "status_colors": {k: _status_color(k) for k in list(action_counts.keys()) + list(content_type_counts.keys())},
        "max_count": max_count,
        "max_ct_count": max_ct_count,
    })


# -----------------------------------------------------------------------
# Onboarded sites view
# -----------------------------------------------------------------------


@router.get("/sites", response_class=HTMLResponse)
async def sites_view(request: Request) -> HTMLResponse:
    """List all onboarded sites with status, activity, and links."""
    _check_auth(request)

    sites: list[dict] = []
    site_activity: dict[str, dict] = {}  # site_id -> {last_change, change_count}

    track_b_db = _find_track_b_db(request)
    if track_b_db:
        try:
            import sqlite3

            conn = sqlite3.connect(str(track_b_db))
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                "SELECT site_id, owner_id, site_url, username, status, active_site_id, created_at "
                "FROM onboarded_sites ORDER BY created_at DESC"
            ).fetchall()
            sites = [dict(r) for r in rows]
            # Mark which site is active per owner.
            for s in sites:
                s["is_active"] = s.get("active_site_id") == s["site_id"]

            # Get last activity per owner (maps to site via owner_id)
            for s in sites:
                owner = s.get("owner_id", "")
                row = conn.execute(
                    "SELECT created_at, content_type, action FROM change_log "
                    "WHERE owner_id = ? ORDER BY created_at DESC LIMIT 1",
                    (owner,),
                ).fetchone()
                if row:
                    site_activity[s["site_id"]] = {
                        "last_change": row["created_at"],
                        "last_action": row["action"],
                        "last_type": row["content_type"],
                    }
                else:
                    site_activity[s["site_id"]] = {
                        "last_change": None,
                        "last_action": None,
                        "last_type": None,
                    }

            conn.close()
        except Exception as exc:
            logger.warning("Could not read Track B sites: %s", exc)

    return _render("dashboard/sites.html", {
        "title": "Onboarded Sites",
        "active_page": "sites",
        "sites": sites,
        "site_activity": site_activity,
    })


# -----------------------------------------------------------------------
# Per-site detail view
# -----------------------------------------------------------------------


@router.get("/sites/{owner_id}", response_class=HTMLResponse)
async def site_detail(
    request: Request,
    owner_id: str,
    content_type: str | None = Query(default=None),
    action: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> HTMLResponse:
    """Full change history for one site (owner), with filters."""
    _check_auth(request)

    # --- site info ---
    site: dict | None = None
    site_stats: dict[str, int] = {"create": 0, "update": 0, "delete": 0, "undo": 0, "failed": 0}
    total_changes = 0
    changes: list[dict] = []

    track_b_db = _find_track_b_db(request)
    if track_b_db:
        try:
            import sqlite3

            conn = sqlite3.connect(str(track_b_db))
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                "SELECT * FROM onboarded_sites WHERE owner_id = ? LIMIT 1",
                (owner_id,),
            ).fetchone()
            if row:
                site = dict(row)

            # Per-site action counts
            rows = conn.execute(
                "SELECT action, COUNT(*) AS n FROM change_log WHERE owner_id = ? GROUP BY action",
                (owner_id,),
            ).fetchall()
            site_stats = {r["action"]: r["n"] for r in rows}

            # Full change history with optional filters
            query = "SELECT * FROM change_log WHERE owner_id = ?"
            params: list[Any] = [owner_id]
            if content_type:
                query += " AND content_type = ?"
                params.append(content_type)
            if action:
                query += " AND action = ?"
                params.append(action)
            if date_from:
                df = _parse_date(date_from)
                if df:
                    query += " AND created_at >= ?"
                    params.append(df)
            if date_to:
                dt = _parse_date(date_to)
                if dt:
                    query += " AND created_at <= ?"
                    params.append(dt + "T23:59:59")
            query += " ORDER BY created_at DESC LIMIT 500"
            rows = conn.execute(query, params).fetchall()
            total_changes = len(rows)
            for r in rows:
                d = dict(r)
                for field in ("before", "after"):
                    if d.get(field) and isinstance(d[field], str):
                        try:
                            d[field] = json.loads(d[field])
                        except Exception:
                            pass
                changes.append(d)

            conn.close()
        except Exception as exc:
            logger.warning("Could not read site detail for %s: %s", owner_id, exc)

    # --- compute after_summaries for the template ---
    for c in changes:
        after_summary = ""
        if c.get("after"):
            a = c["after"]
            if isinstance(a, dict):
                after_summary = str(a.get("title", a.get("phone", a.get("hours", ""))))[:60]
        c["after_summary"] = after_summary

    return _render("dashboard/site_detail.html", {
        "title": f"Site History: {owner_id}",
        "active_page": "sites",
        "owner_id": owner_id,
        "site": site,
        "site_stats": site_stats,
        "total_changes": total_changes,
        "changes": changes,
        "content_type": content_type,
        "action": action,
        "date_from": date_from,
        "date_to": date_to,
    })


# -----------------------------------------------------------------------
# Change log view with date range filtering
# -----------------------------------------------------------------------


@router.get("/changes", response_class=HTMLResponse)
async def changes_view(
    request: Request,
    owner_id: str | None = Query(default=None),
    content_type: str | None = Query(default=None),
    action: str | None = Query(default=None),
    change_id: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> HTMLResponse:
    """Searchable/filterable change log with date range."""
    _check_auth(request)

    changes: list[dict] = []
    track_b_db = _find_track_b_db(request)
    if track_b_db:
        try:
            import sqlite3

            conn = sqlite3.connect(str(track_b_db))
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM change_log WHERE 1=1"
            params: list[Any] = []

            if owner_id:
                query += " AND owner_id = ?"
                params.append(owner_id)
            if content_type:
                query += " AND content_type = ?"
                params.append(content_type)
            if action:
                query += " AND action = ?"
                params.append(action)
            if change_id:
                query += " AND change_id LIKE ?"
                params.append(f"%{change_id}%")
            if date_from:
                df = _parse_date(date_from)
                if df:
                    query += " AND created_at >= ?"
                    params.append(df)
            if date_to:
                dt = _parse_date(date_to)
                if dt:
                    query += " AND created_at <= ?"
                    params.append(dt + "T23:59:59")

            query += " ORDER BY created_at DESC LIMIT 200"
            rows = conn.execute(query, params).fetchall()
            for r in rows:
                d = dict(r)
                for field in ("before", "after"):
                    if d.get(field) and isinstance(d[field], str):
                        try:
                            d[field] = json.loads(d[field])
                        except Exception:
                            pass
                changes.append(d)
            conn.close()
        except Exception as exc:
            logger.warning("Could not read Track B changes: %s", exc)

    # --- compute after_summaries ---
    for c in changes:
        after_summary = ""
        if c.get("after"):
            a = c["after"]
            if isinstance(a, dict):
                after_summary = str(a.get("title", a.get("phone", a.get("hours", ""))))[:50]
        c["after_summary"] = after_summary

    # Build CSV export URL
    csv_params = []
    if owner_id:
        csv_params.append(f"owner_id={owner_id}")
    if content_type:
        csv_params.append(f"content_type={content_type}")
    if action:
        csv_params.append(f"action={action}")
    if change_id:
        csv_params.append(f"change_id={change_id}")
    if date_from:
        csv_params.append(f"date_from={date_from}")
    if date_to:
        csv_params.append(f"date_to={date_to}")
    csv_url = "/admin/dashboard/changes.csv?" + "&".join(csv_params)

    return _render("dashboard/changes.html", {
        "title": "Change Log",
        "active_page": "changes",
        "changes": changes,
        "owner_id": owner_id,
        "selected_content_type": content_type,
        "selected_action": action,
        "change_id": change_id,
        "date_from": date_from,
        "date_to": date_to,
        "csv_url": csv_url,
    })


# -----------------------------------------------------------------------
# CSV export
# -----------------------------------------------------------------------


@router.get("/changes.csv")
async def changes_csv(
    request: Request,
    owner_id: str | None = Query(default=None),
    content_type: str | None = Query(default=None),
    action: str | None = Query(default=None),
    change_id: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> StreamingResponse:
    """Export change log as CSV."""
    _check_auth(request)

    changes: list[dict] = []
    track_b_db = _find_track_b_db(request)
    if track_b_db:
        try:
            import sqlite3

            conn = sqlite3.connect(str(track_b_db))
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM change_log WHERE 1=1"
            params: list[Any] = []
            if owner_id:
                query += " AND owner_id = ?"
                params.append(owner_id)
            if content_type:
                query += " AND content_type = ?"
                params.append(content_type)
            if action:
                query += " AND action = ?"
                params.append(action)
            if change_id:
                query += " AND change_id LIKE ?"
                params.append(f"%{change_id}%")
            if date_from:
                df = _parse_date(date_from)
                if df:
                    query += " AND created_at >= ?"
                    params.append(df)
            if date_to:
                dt = _parse_date(date_to)
                if dt:
                    query += " AND created_at <= ?"
                    params.append(dt + "T23:59:59")
            query += " ORDER BY created_at DESC LIMIT 1000"
            changes = [dict(r) for r in conn.execute(query, params).fetchall()]
            conn.close()
        except Exception:
            pass

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["change_id", "owner_id", "content_type", "action", "live_url", "created_at"])
    for c in changes:
        writer.writerow(
            [
                c.get("change_id", ""),
                c.get("owner_id", ""),
                c.get("content_type", ""),
                c.get("action", ""),
                c.get("live_url", ""),
                c.get("created_at", ""),
            ]
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=changes.csv"},
    )


# -----------------------------------------------------------------------
# Failures view with date range
# -----------------------------------------------------------------------


@router.get("/failures", response_class=HTMLResponse)
async def failures_view(
    request: Request,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> HTMLResponse:
    """Recent failed writes — the cases most likely needing attention."""
    _check_auth(request)

    failures: list[dict] = []
    track_b_db = _find_track_b_db(request)
    if track_b_db:
        try:
            import sqlite3

            conn = sqlite3.connect(str(track_b_db))
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM change_log WHERE action = 'failed'"
            params: list[Any] = []
            if date_from:
                df = _parse_date(date_from)
                if df:
                    query += " AND created_at >= ?"
                    params.append(df)
            if date_to:
                dt = _parse_date(date_to)
                if dt:
                    query += " AND created_at <= ?"
                    params.append(dt + "T23:59:59")
            query += " ORDER BY created_at DESC LIMIT 50"
            failures = [dict(r) for r in conn.execute(query, params).fetchall()]
            conn.close()
        except Exception:
            pass

    # --- compute error_message for the template ---
    for f in failures:
        error = ""
        after = f.get("after")
        if after and isinstance(after, str):
            try:
                after = json.loads(after)
            except Exception:
                pass
        if isinstance(after, dict):
            error = after.get("error_message", "")
        f["error_message"] = str(error)

    return _render("dashboard/failures.html", {
        "title": "⚠ Failed Writes",
        "active_page": "failures",
        "failures": failures,
        "date_from": date_from,
        "date_to": date_to,
    })


# -----------------------------------------------------------------------
# Escalations redirect
# -----------------------------------------------------------------------


@router.get("/escalations")
async def escalations_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin", status_code=303)


# -----------------------------------------------------------------------
# JSON API endpoints
# -----------------------------------------------------------------------


@router.get("/api/metrics")
async def api_metrics(
    request: Request,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> dict[str, Any]:
    """Usage metrics with optional date range."""
    _check_auth(request)
    db_path: Path = request.app.state.settings.db_path

    open_esc = count_open_escalations(db_path)
    total_esc = count_escalation_requests(db_path)

    action_counts: dict[str, int] = {}
    content_type_counts: dict[str, int] = {}
    track_b_db = _find_track_b_db(request)
    if track_b_db:
        try:
            import sqlite3

            conn = sqlite3.connect(str(track_b_db))
            query_base = "SELECT action, COUNT(*) AS n FROM change_log"
            params: list[Any] = []
            wheres = []
            if date_from:
                df = _parse_date(date_from)
                if df:
                    wheres.append("created_at >= ?")
                    params.append(df)
            if date_to:
                dt = _parse_date(date_to)
                if dt:
                    wheres.append("created_at <= ?")
                    params.append(dt + "T23:59:59")
            where = (" WHERE " + " AND ".join(wheres)) if wheres else ""

            rows = conn.execute(f"{query_base}{where} GROUP BY action", params).fetchall()
            action_counts = {r[0]: r[1] for r in rows}

            rows = conn.execute(
                f"SELECT content_type, COUNT(*) AS n FROM change_log{where} GROUP BY content_type",
                params,
            ).fetchall()
            content_type_counts = {r[0]: r[1] for r in rows}
            conn.close()
        except Exception:
            pass

    return {
        "escalations": {"open": open_esc, "total": total_esc},
        "changes": action_counts,
        "content_types": content_type_counts,
    }


@router.get("/api/health")
async def api_health(request: Request) -> dict[str, Any]:
    _check_auth(request)
    track_b_ok = False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{TRACK_B_URL}/health")
            track_b_ok = resp.status_code == 200
    except Exception:
        pass
    return {"track_a": True, "track_b": track_b_ok}


@router.get("/api/refresh")
async def api_refresh(request: Request) -> dict[str, Any]:
    """Lightweight AJAX endpoint for auto-refresh — returns only card values."""
    _check_auth(request)
    db_path: Path = request.app.state.settings.db_path

    result: dict[str, Any] = {
        "sites_active": 0,
        "sites_count": 0,
        "open_esc": count_open_escalations(db_path),
        "total_esc": count_escalation_requests(db_path),
        "failures_count": 0,
        "undo_count": 0,
    }

    track_b_db = _find_track_b_db(request)
    if track_b_db:
        try:
            import sqlite3

            conn = sqlite3.connect(str(track_b_db))
            row = conn.execute("SELECT COUNT(*) AS n FROM onboarded_sites").fetchone()
            result["sites_count"] = row[0] if row else 0
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM onboarded_sites WHERE status='active'"
            ).fetchone()
            result["sites_active"] = row[0] if row else 0
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM change_log WHERE action='failed'"
            ).fetchone()
            result["failures_count"] = row[0] if row else 0
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM change_log WHERE action='undo'"
            ).fetchone()
            result["undo_count"] = row[0] if row else 0
            conn.close()
        except Exception:
            pass

    return result


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


@router.get("/api/activity")
async def api_activity(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    since: str | None = Query(default=None),
) -> dict[str, Any]:
    """Recent changes for the live activity feed.

    `since` is an ISO timestamp — only return changes newer than this.
    Used by the frontend to poll for new activity without re-fetching
    everything.
    """
    _check_auth(request)

    changes: list[dict] = []
    track_b_db = _find_track_b_db(request)
    if track_b_db:
        try:
            import sqlite3

            conn = sqlite3.connect(str(track_b_db))
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM change_log"
            params: list[Any] = []
            if since:
                query += " WHERE created_at > ?"
                params.append(since)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            for r in rows:
                d = dict(r)
                # Parse after field for summary
                after = d.get("after")
                if after and isinstance(after, str):
                    try:
                        after = json.loads(after)
                    except Exception:
                        pass
                summary = ""
                if isinstance(after, dict):
                    summary = str(after.get("title", after.get("phone", after.get("hours", ""))))[
                        :60
                    ]
                changes.append(
                    {
                        "change_id": d.get("change_id", ""),
                        "owner_id": d.get("owner_id", ""),
                        "content_type": d.get("content_type", ""),
                        "action": d.get("action", ""),
                        "summary": summary,
                        "created_at": str(d.get("created_at", "")),
                    }
                )
            conn.close()
        except Exception as exc:
            logger.debug("Could not read activity: %s", exc)

    return {"changes": changes, "count": len(changes)}


def _find_track_b_db(request: Request) -> Path | None:
    track_a_db: Path = request.app.state.settings.db_path
    track_b_db = track_a_db.parent / "trackb.db"
    if track_b_db.exists():
        return track_b_db
    for candidate in [
        track_a_db.parent.parent / "track-b" / "data" / "trackb.db",
        Path("track-b/data/trackb.db"),
    ]:
        if candidate.exists():
            return candidate
    return None
