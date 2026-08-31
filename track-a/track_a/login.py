"""Admin login page with username/password authentication.

Uses HMAC-signed session cookies so no database or session store is needed.
Credentials come from ADMIN_USERNAME / ADMIN_PASSWORD env vars.
Falls back to ADMIN_TOKEN bearer auth for API consumers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger("track_a.login")

router = APIRouter(tags=["login"])

COOKIE_NAME = "wpbot_session"
COOKIE_MAX_AGE = 86400 * 7  # 7 days

# --- Rate limiting for login attempts ----------------------------------------
# Tracks failed attempts per IP.  After _LOGIN_MAX_ATTEMPTS failures within
# _LOGIN_WINDOW_SECONDS the IP is locked out for _LOGIN_LOCKOUT_SECONDS.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 900  # 15 minutes
_LOGIN_LOCKOUT_SECONDS = 900  # 15 minutes

# {ip: [(timestamp, ...)] }
_failed_attempts: dict[str, list[float]] = defaultdict(list)
_lockouts: dict[str, float] = {}  # {ip: lockout_expiry}


def _client_ip(request: Request) -> str:
    """Extract the real client IP, respecting X-Forwarded-For from proxies."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_rate_limited(ip: str) -> bool:
    """Return True if the IP is currently locked out."""
    now = time.time()
    # Check lockout
    expiry = _lockouts.get(ip)
    if expiry and now < expiry:
        return True
    # Clean expired lockout
    if expiry and now >= expiry:
        del _lockouts[ip]
        _failed_attempts.pop(ip, None)
    return False


def _record_failure(ip: str) -> None:
    """Record a failed login attempt. Triggers lockout if threshold exceeded."""
    now = time.time()
    # Prune old attempts outside the window
    _failed_attempts[ip] = [
        t for t in _failed_attempts[ip] if now - t < _LOGIN_WINDOW_SECONDS
    ]
    _failed_attempts[ip].append(now)
    if len(_failed_attempts[ip]) >= _LOGIN_MAX_ATTEMPTS:
        _lockouts[ip] = now + _LOGIN_LOCKOUT_SECONDS
        logger.warning(
            "login rate-limited: IP %s locked out for %ds after %d failures",
            ip, _LOGIN_LOCKOUT_SECONDS, len(_failed_attempts[ip]),
        )


def _clear_failures(ip: str) -> None:
    """Clear failed attempts on successful login."""
    _failed_attempts.pop(ip, None)
    _lockouts.pop(ip, None)


def _sign(value: str, secret: str) -> str:
    """HMAC-SHA256 sign a value."""
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _get_secret() -> str:
    """Return the signing secret (ADMIN_PASSWORD or ADMIN_TOKEN)."""
    return os.environ.get("ADMIN_PASSWORD", "") or os.environ.get("ADMIN_TOKEN", "")


def create_session_cookie(username: str) -> str:
    """Create a signed session cookie value: username|expiry|signature."""
    expiry = str(int(time.time()) + COOKIE_MAX_AGE)
    payload = f"{username}|{expiry}"
    sig = _sign(payload, _get_secret())
    return f"{payload}|{sig}"


def validate_session_cookie(value: str) -> str | None:
    """Validate a session cookie. Returns the username if valid, else None."""
    secret = _get_secret()
    if not secret:
        return None
    parts = value.split("|")
    if len(parts) != 3:
        return None
    username, expiry_str, provided_sig = parts
    try:
        if int(expiry_str) < int(time.time()):
            return None  # expired
    except ValueError:
        return None
    expected_sig = _sign(f"{username}|{expiry_str}", secret)
    if not hmac.compare_digest(provided_sig, expected_sig):
        return None
    return username


def check_auth(request: Request) -> str | None:
    """Check auth from session cookie or bearer token. Returns username or None."""
    # 1. Check session cookie (browser login)
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    if cookie_val:
        username = validate_session_cookie(cookie_val)
        if username:
            return username

    # 2. Check bearer token (API access via ADMIN_TOKEN)
    token = os.environ.get("ADMIN_TOKEN", "") or getattr(request.app.state, "admin_token", "")
    if token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
            if hmac.compare_digest(provided, token):
                return "api"
        query_token = request.query_params.get("token", "")
        if query_token and hmac.compare_digest(query_token, token):
            return "api"

    return None


async def require_admin(request: Request) -> str:
    """FastAPI dependency that rejects unauthenticated requests with 401.

    Use it on any endpoint that should only be accessible to admins:

        @app.get("/secret")
        async def secret(admin: str = Depends(require_admin)):
            ...
    """
    user = check_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sitepaw Admin Login</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🐾</text></svg>">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f5; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
    .login-card { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.08); width: 100%; max-width: 400px; }
    .login-card h1 { font-size: 1.5em; color: #0B1220; margin-bottom: 8px; }
    .login-card p { color: #64748B; font-size: 0.9em; margin-bottom: 24px; }
    .form-group { margin-bottom: 16px; }
    .form-group label { display: block; font-size: 0.85em; font-weight: 600; color: #333; margin-bottom: 6px; }
    .form-group input { width: 100%; padding: 10px 14px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; outline: none; transition: border-color 0.2s; }
    .form-group input:focus { border-color: #0D9488; }
    .btn { width: 100%; padding: 12px; background: #0D9488; color: white; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
    .btn:hover { background: #0a7a70; }
    .error { background: #fef2f2; border: 1px solid #fecaca; color: #dc2626; padding: 10px 14px; border-radius: 8px; font-size: 0.85em; margin-bottom: 16px; }
    .rate-limit { background: #fefce8; border: 1px solid #fde68a; color: #92400e; padding: 10px 14px; border-radius: 8px; font-size: 0.85em; margin-bottom: 16px; }
  </style>
</head>
<body>
  <div class="login-card">
    <h1>🐾 Sitepaw Admin</h1>
    <p>Sign in to access the dashboard.</p>
    {error_html}
    <form method="POST" action="/admin/login">
      <div class="form-group">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" required autofocus>
      </div>
      <div class="form-group">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" required>
      </div>
      <button type="submit" class="btn">Sign In</button>
    </form>
  </div>
</body>
</html>"""


@router.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Show the login form."""
    # Already logged in? Redirect to dashboard.
    if check_auth(request):
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    return HTMLResponse(content=_LOGIN_PAGE.replace("{error_html}", ""))


@router.post("/admin/login")
async def login_submit(request: Request) -> HTMLResponse:
    """Process login form submission."""
    ip = _client_ip(request)

    # Check rate limit before processing
    if _is_rate_limited(ip):
        logger.warning("login blocked: IP %s is rate-limited", ip)
        error = '<div class="rate-limit">Too many failed attempts. Please try again later.</div>'
        return HTMLResponse(content=_LOGIN_PAGE.replace("{error_html}", error), status_code=429)

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    expected_user = os.environ.get("ADMIN_USERNAME", "")
    expected_pass = os.environ.get("ADMIN_PASSWORD", "")

    if not expected_user or not expected_pass:
        error = '<div class="error">Admin credentials not configured. Set ADMIN_USERNAME and ADMIN_PASSWORD.</div>'
        return HTMLResponse(content=_LOGIN_PAGE.replace("{error_html}", error))

    if username == expected_user and hmac.compare_digest(password, expected_pass):
        _clear_failures(ip)
        cookie_val = create_session_cookie(username)
        response = RedirectResponse(url="/admin/dashboard", status_code=302)
        response.set_cookie(
            COOKIE_NAME,
            cookie_val,
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response

    _record_failure(ip)
    remaining = _LOGIN_MAX_ATTEMPTS - len(_failed_attempts.get(ip, []))
    if remaining <= 0:
        error = '<div class="rate-limit">Too many failed attempts. Locked out for 15 minutes.</div>'
    else:
        error = f'<div class="error">Invalid username or password. ({remaining} attempts remaining)</div>'
    return HTMLResponse(content=_LOGIN_PAGE.replace("{error_html}", error))


@router.get("/admin/logout")
async def logout() -> RedirectResponse:
    """Clear session and redirect to login."""
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response
