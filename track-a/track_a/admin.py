"""Internal admin view for escalation requests.

A minimal server-rendered interface for Justice to review and act on
escalation requests logged by Track A's conversation flow (PRD §10).

Auth: simple bearer token via ``ADMIN_TOKEN`` env var.  Not production
auth — just enough to keep random visitors out during the pilot.

Routes:
    GET  /admin                — escalation list (newest first, status filter)
    GET  /admin/{id}           — escalation detail + update form
    POST /admin/{id}/update    — update status / notes
    GET  /admin/api/count      — JSON: {open: N, total: M} (for dashboards)
"""

from __future__ import annotations

import html
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .store import (
    count_escalation_requests,
    count_open_escalations,
    get_escalation_request,
    list_escalation_requests,
    update_escalation_status,
)

logger = logging.getLogger("track_a.admin")

router = APIRouter(prefix="/admin", tags=["admin"])

# Valid escalation statuses
_VALID_STATUSES = {"new", "in_progress", "resolved"}


def _check_auth(request: Request) -> None:
    """Verify the admin token from the Authorization header or query param."""
    token = getattr(request.app.state, "admin_token", None) or os.environ.get("ADMIN_TOKEN", "")
    if not token:
        return  # No token configured → auth disabled (dev mode)

    # Check Authorization header first, then query param
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        provided = auth_header[7:]
    else:
        provided = request.query_params.get("token", "")

    if provided != token:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _status_badge(status: str) -> str:
    """HTML badge for a status value."""
    colors = {
        "new": "#e74c3c",
        "in_progress": "#f39c12",
        "resolved": "#27ae60",
    }
    color = colors.get(status, "#95a5a6")
    return (
        f'<span style="background:{color};color:white;padding:2px 8px;'
        f'border-radius:4px;font-size:0.85em;">{html.escape(status)}</span>'
    )


def _escap(s: str | None) -> str:
    """HTML-escape a potentially None string."""
    return html.escape(s or "")


# -----------------------------------------------------------------------
# HTML routes
# -----------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def escalation_list(
    request: Request,
    status: str | None = Query(default=None, description="Filter by status"),
) -> HTMLResponse:
    """List escalation requests, newest first."""
    _check_auth(request)

    db_path: Path = request.app.state.settings.db_path
    open_count = count_open_escalations(db_path)
    total_count = count_escalation_requests(db_path)
    escalations = list_escalation_requests(db_path, status=status)

    # Build filter links
    filter_links = (
        '<a href="/admin" style="margin-right:8px;">All</a>'
        '<a href="/admin?status=new" style="margin-right:8px;">New</a>'
        '<a href="/admin?status=in_progress" style="margin-right:8px;">In Progress</a>'
        '<a href="/admin?status=resolved">Resolved</a>'
    )

    # Build table rows
    rows = ""
    for e in escalations:
        msg_preview = _escap((e.get("original_message") or "")[:80])
        if len(e.get("original_message") or "") > 80:
            msg_preview += "…"
        rows += f"""
        <tr>
            <td>{e["id"]}</td>
            <td>{_escap(e.get("owner_phone"))}</td>
            <td>{msg_preview}</td>
            <td>{_status_badge(e.get("status", "new"))}</td>
            <td>{_escap(e.get("created_at", "")[:19])}</td>
            <td><a href="/admin/{e["id"]}">View</a></td>
        </tr>
        """

    if not rows:
        rows = '<tr><td colspan="6" style="text-align:center;color:#888;">No escalation requests found.</td></tr>'

    page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>WP-Bot Admin — Escalations</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 20px; background: #f5f5f5; }}
        h1 {{ color: #2c3e50; }}
        .count-bar {{ background: white; padding: 12px 20px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .count-bar span {{ margin-right: 20px; }}
        .count-num {{ font-weight: bold; font-size: 1.2em; }}
        .open-num {{ color: #e74c3c; }}
        .filters {{ margin-bottom: 12px; }}
        table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #2c3e50; color: white; }}
        tr:hover {{ background: #f8f9fa; }}
        a {{ color: #3498db; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <h1>Escalation Requests</h1>
    <div class="count-bar">
        <span>Open: <span class="count-num open-num">{open_count}</span></span>
        <span>Total: <span class="count-num">{total_count}</span></span>
    </div>
    <div class="filters">{filter_links}</div>
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Owner</th>
                <th>Message</th>
                <th>Status</th>
                <th>Created</th>
                <th>Action</th>
            </tr>
        </thead>
        <tbody>
            {rows}
        </tbody>
    </table>
</body>
</html>"""
    return HTMLResponse(content=page)


@router.get("/{escalation_id}", response_class=HTMLResponse)
async def escalation_detail(request: Request, escalation_id: int) -> HTMLResponse:
    """View and update a single escalation request."""
    _check_auth(request)

    db_path: Path = request.app.state.settings.db_path
    esc = get_escalation_request(db_path, escalation_id)
    if esc is None:
        raise HTTPException(status_code=404, detail="Escalation not found")

    status_options = ""
    for s in _VALID_STATUSES:
        selected = "selected" if esc.get("status") == s else ""
        status_options += f'<option value="{s}" {selected}>{s}</option>'

    page = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Escalation #{esc["id"]} — WP-Bot Admin</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 20px; background: #f5f5f5; }}
        h1 {{ color: #2c3e50; }}
        .card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); max-width: 700px; }}
        .field {{ margin-bottom: 12px; }}
        .label {{ font-weight: bold; color: #555; }}
        .value {{ margin-top: 4px; }}
        textarea, select {{ width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }}
        textarea {{ min-height: 80px; resize: vertical; }}
        button {{ background: #3498db; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; font-size: 14px; }}
        button:hover {{ background: #2980b9; }}
        a {{ color: #3498db; text-decoration: none; }}
    </style>
</head>
<body>
    <h1>Escalation #{esc["id"]}</h1>
    <a href="/admin">← Back to list</a>
    <div class="card" style="margin-top: 16px;">
        <div class="field">
            <div class="label">Owner Phone</div>
            <div class="value">{_escap(esc.get("owner_phone"))}</div>
        </div>
        <div class="field">
            <div class="label">Original Message</div>
            <div class="value" style="background:#f8f9fa;padding:10px;border-radius:4px;">
                {_escap(esc.get("original_message"))}
            </div>
        </div>
        <div class="field">
            <div class="label">Created</div>
            <div class="value">{_escap(esc.get("created_at", "")[:19])}</div>
        </div>
        <div class="field">
            <div class="label">Current Status</div>
            <div class="value">{_status_badge(esc.get("status", "new"))}</div>
        </div>
        <form method="POST" action="/admin/{esc["id"]}/update">
            <div class="field">
                <div class="label">Update Status</div>
                <select name="status">{status_options}</select>
            </div>
            <div class="field">
                <div class="label">Notes (what was done)</div>
                <textarea name="notes" placeholder="e.g. Called the owner, quoted $500 for the homepage redesign…">{_escap(esc.get("notes"))}</textarea>
            </div>
            <button type="submit">Save</button>
        </form>
    </div>
</body>
</html>"""
    return HTMLResponse(content=page)


@router.post("/{escalation_id}/update")
async def update_escalation(
    request: Request,
    escalation_id: int,
) -> RedirectResponse:
    """Update an escalation request's status and notes."""
    _check_auth(request)

    db_path: Path = request.app.state.settings.db_path
    form = await request.form()
    status = str(form.get("status", "new"))
    notes = str(form.get("notes", "")) or None

    if status not in _VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    updated = update_escalation_status(db_path, escalation_id, status=status, notes=notes)
    if not updated:
        raise HTTPException(status_code=404, detail="Escalation not found")

    return RedirectResponse(url=f"/admin/{escalation_id}", status_code=303)


# -----------------------------------------------------------------------
# JSON API (for dashboards / future Stage 4)
# -----------------------------------------------------------------------


@router.get("/api/count")
async def api_count(request: Request) -> dict[str, int]:
    """Return open and total escalation counts."""
    _check_auth(request)
    db_path: Path = request.app.state.settings.db_path
    return {
        "open": count_open_escalations(db_path),
        "total": count_escalation_requests(db_path),
    }
