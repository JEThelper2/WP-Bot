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

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader

from .login import check_auth
from .store import (
    count_escalation_requests,
    count_open_escalations,
    get_escalation_request,
    list_escalation_requests,
    update_escalation_status,
)

logger = logging.getLogger("track_a.admin")

router = APIRouter(prefix="/admin", tags=["admin"])

# Jinja2 environment for templates
_template_dir = Path(__file__).resolve().parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(_template_dir),
    autoescape=True,  # Auto-escape HTML to prevent XSS
)

# Valid escalation statuses
_VALID_STATUSES = {"new", "in_progress", "resolved"}

# Status color mapping for templates
_STATUS_COLORS = {
    "new": "#e74c3c",
    "in_progress": "#f39c12",
    "resolved": "#27ae60",
}


def _check_auth(request: Request) -> None:
    """Verify admin credentials via session cookie or bearer token."""
    if not check_auth(request):
        # No credentials configured → auth disabled (dev mode)
        import os
        if not os.environ.get("ADMIN_USERNAME") and not os.environ.get("ADMIN_TOKEN") and not getattr(request.app.state, "admin_token", None):
            return
        raise HTTPException(status_code=401, detail="Unauthorized")


def _render(template_name: str, context: dict) -> str:
    """Render a Jinja2 template with the given context."""
    template = _jinja_env.get_template(template_name)
    return template.render(**context)


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

    page = _render("admin/escalation_list.html", {
        "title": "Escalation Requests",
        "active_page": "escalations",
        "open_count": open_count,
        "total_count": total_count,
        "escalations": escalations,
        "status_colors": _STATUS_COLORS,
    })
    return HTMLResponse(content=page)


@router.get("/{escalation_id}", response_class=HTMLResponse)
async def escalation_detail(request: Request, escalation_id: int) -> HTMLResponse:
    """View and update a single escalation request."""
    _check_auth(request)

    db_path: Path = request.app.state.settings.db_path
    esc = get_escalation_request(db_path, escalation_id)
    if esc is None:
        raise HTTPException(status_code=404, detail="Escalation not found")

    page = _render("admin/escalation_detail.html", {
        "title": f"Escalation #{esc['id']}",
        "active_page": "escalations",
        "esc": esc,
        "valid_statuses": sorted(_VALID_STATUSES),
        "status_colors": _STATUS_COLORS,
    })
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
