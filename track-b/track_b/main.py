"""WP-Bot Track B — WordPress site/state service (real API).

Endpoints:

- `POST /intent` — the contract endpoint. Accepts an intent object
  (validated against intent.schema.json; the body may be accompanied by
  `?decision=yes|no` to RESOLVE a previously staged confirmation):
  - no decision → **stage** the intent as pending (B3, Redis, 15min TTL)
    and return `status: "needs_confirmation"` with the pending change_id;
  - `decision=yes` → resolve the pending confirmation and, if it was
    released, run B2 allowlist → B1 WordPress write → B4 change log,
    returning a success result with real before/after/live_url;
  - `decision=no` → discard the pending state; nothing is written.
  Every response is a result object matching result.schema.json,
  validated with `validate_result()` before responding.
- `POST /undo` — accepts `{owner_id}`, routes through B4: reverse-applies
  the owner's most recent change and logs the undo itself.
- `POST /sites/onboard` — PRD §12 step 3 (B5).

Services (site store, pending store, change log, client factory) are
injected for tests or built from settings (the default builder uses Redis
for pending and Postgres for the change log when `WPBOT_PG_DSN` is set;
otherwise an in-memory dev log is used with a warning).

Track A's pre-Integration-Phase tests keep using `track_b.stub`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared_contract import (
    CONTRACT_VERSION,
    ContractValidationError,
    validate_intent,
    validate_result,
)
from shared_contract.logging import setup_logging

from .allowlist import apply_intent
from .changelog import ChangeLog, InMemoryChangeLog
from .config import Settings
from .onboarding import OnboardedSiteStore, onboard_site
from .pending import InMemoryPendingStore, PendingStore, RedisPendingStore
from .undo import UNDO_WINDOW_SECONDS, undo
from .wordpress import WordPressClient

logger = logging.getLogger("track_b.api")


class OnboardRequest(BaseModel):
    """Request to onboard a WordPress site."""

    site_url: str
    username: str
    app_password: str
    owner_id: str


class UndoRequest(BaseModel):
    """Request to undo the most recent change for an owner."""

    owner_id: str
    site_id: str | None = None  # multi-site: scope undo to this site


class IntentRequest(BaseModel):
    """An intent object matching shared-contract/intent.schema.json."""

    contract_version: str
    owner_id: str
    action: str
    content_type: str
    fields: dict[str, Any]
    confidence: float
    site_id: str | None = None  # multi-site: target site id


@dataclass
class TrackBServices:
    sites: OnboardedSiteStore
    pending: PendingStore
    changelog: ChangeLog
    make_client: Callable[[Any], WordPressClient]


async def build_default_services(settings: Settings) -> TrackBServices:
    """Production wiring: SQLite site store, Redis pending, PG change log."""
    import redis.asyncio as aioredis

    sites = OnboardedSiteStore(settings.db_path)

    if settings.pg_dsn:
        from .changelog import PostgresChangeLog

        changelog: ChangeLog = await PostgresChangeLog.connect(settings.pg_dsn)
    else:
        changelog = InMemoryChangeLog()
        logger.warning(
            "WPBOT_PG_DSN not set — using an in-memory change log (PRD §11 "
            "durability requires Postgres in production)"
        )

    # Pending store: Redis in production; in-memory dev fallback when Redis
    # is unreachable (confirmations then aren't shared across workers).
    try:
        redis_client = aioredis.from_url(settings.redis_url, socket_connect_timeout=2.0)
        await redis_client.ping()
        pending: PendingStore = RedisPendingStore(redis_client)
    except Exception as exc:
        logger.warning(
            "Redis unreachable at %s (%s) — using an in-memory pending store",
            settings.redis_url,
            exc,
        )
        pending = InMemoryPendingStore()

    def make_client(site: Any) -> WordPressClient:
        creds = sites.credentials_for(site.site_id)
        if creds is None:
            raise RuntimeError(f"no credentials stored for site {site.site_id}")
        return WordPressClient(site.site_url, creds[0], creds[1])

    return TrackBServices(
        sites=sites,
        pending=pending,
        changelog=changelog,
        make_client=make_client,
    )


def _result(
    status: str,
    change_id: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    live_url: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    result = {
        "contract_version": CONTRACT_VERSION,
        "status": status,
        "change_id": change_id,
        "before": before,
        "after": after,
        "live_url": live_url,
        "error_message": error_message,
    }
    validate_result(result)  # boundary discipline: never emit a bad result
    return result


def _respond(result: dict[str, Any], *, failed_status: int = 422) -> JSONResponse:
    code = failed_status if result["status"] == "failed" else 200
    return JSONResponse(result, status_code=code)


def create_app(
    settings: Settings | None = None,
    services: TrackBServices | None = None,
    onboarding_runner: Callable | None = None,
) -> FastAPI:
    setup_logging()
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="Sitepaw Track B (WordPress site/state)",
        version="0.1.0",
        description=(
            "Real Track B API: intent staging/resolution, WordPress writes "
            "through the B2 allowlist, the PRD §11 change log + undo, and "
            "site onboarding."
        ),
    )
    app.state.settings = settings
    app.state.services = services
    app.state._services_lock = __import__("asyncio").Lock()

    async def get_services() -> TrackBServices:
        if app.state.services is None:
            async with app.state._services_lock:
                # Double-check after acquiring the lock.
                if app.state.services is None:
                    app.state.services = await build_default_services(settings)
        return app.state.services

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Liveness + readiness: checks SQLite, Redis, and Postgres."""
        import sqlite3

        checks: dict[str, str] = {}
        # SQLite site store
        try:
            conn = sqlite3.connect(str(settings.db_path))
            conn.execute("SELECT 1")
            conn.close()
            checks["sqlite"] = "ok"
        except Exception:
            checks["sqlite"] = "unreachable"
        # Redis
        try:
            import redis.asyncio as aioredis

            r = aioredis.from_url(settings.redis_url, socket_connect_timeout=1.0)
            await r.ping()
            await r.aclose()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unavailable"
        # Postgres (only if configured)
        if settings.pg_dsn:
            try:
                import asyncpg

                conn = await asyncpg.connect(settings.pg_dsn, timeout=2.0)
                await conn.execute("SELECT 1")
                await conn.close()
                checks["postgres"] = "ok"
            except Exception:
                checks["postgres"] = "unreachable"
        else:
            checks["postgres"] = "not_configured"
        all_ok = all(v in ("ok", "not_configured", "unavailable") for v in checks.values())
        return {"status": "ok" if all_ok else "degraded", "service": "track-b", **checks}

    # ------------------------------------------------------- onboarding

    @app.post("/sites/onboard", response_model=None)
    async def onboard(payload: OnboardRequest) -> dict[str, Any]:
        if onboarding_runner is not None:
            result = await onboarding_runner(
                payload.site_url, payload.username, payload.app_password, payload.owner_id
            )
        else:
            services = await get_services()
            result = await onboard_site(
                payload.site_url,
                payload.username,
                payload.app_password,
                payload.owner_id,
                store=services.sites,
            )
        if result.status == "success":
            return {
                "status": "success",
                "reason": result.reason,
                "site_id": result.site_id,
                "site_url": result.site_url,
            }
        raise HTTPException(
            status_code=422,
            detail={
                "status": "failed",
                "reason": result.reason,
                "message": result.message,
            },
        )

    # ------------------------------------------------------- intent

    @app.post("/intent", response_model=None)
    async def intent_endpoint(
        payload: dict[str, Any] = Body(..., description="Intent object per shared contract"),
        decision: str | None = Query(default=None, description="'yes' or 'no' to resolve a staged confirmation"),
    ) -> JSONResponse:
        """Stage a confirmation-ready intent, or resolve a staged one.

        `decision=yes|no` marks this call as a resolution of the owner's
        pending confirmation (B3). Without it, the intent is staged.
        """
        try:
            validate_intent(payload)
        except ContractValidationError as exc:
            return _respond(
                _result("failed", f"ch-{uuid.uuid4().hex[:12]}", error_message=str(exc))
            )

        services = await get_services()
        owner_id = payload["owner_id"]

        if decision is not None:
            return await _resolve(owner_id, decision, services)

        # ---- staging (B3) ----
        try:
            change_id = await services.pending.stage_pending(payload)
        except ContractValidationError as exc:
            return _respond(
                _result("failed", f"ch-{uuid.uuid4().hex[:12]}", error_message=str(exc))
            )
        return _respond(_result("needs_confirmation", change_id))

    async def _resolve(owner_id: str, decision: str, services: TrackBServices) -> JSONResponse:
        try:
            outcome = await services.pending.resolve_pending(owner_id, decision)
        except ValueError as exc:  # invalid decision value
            return _respond(
                _result("failed", f"ch-{uuid.uuid4().hex[:12]}", error_message=str(exc))
            )

        if outcome.kind == "discarded":
            # NO: nothing was written. Track A already told the owner.
            return _respond(_result("success", outcome.change_id or f"ch-{uuid.uuid4().hex[:12]}"))
        if outcome.kind == "nothing_pending":
            return _respond(
                _result("failed", f"ch-{uuid.uuid4().hex[:12]}", error_message=outcome.message)
            )
        if outcome.kind == "expired":
            return _respond(
                _result(
                    "failed",
                    outcome.change_id or f"ch-{uuid.uuid4().hex[:12]}",
                    error_message=outcome.message,
                )
            )

        # released (YES): B2 -> B1 -> B4, using the staged change_id so the
        # audit trail links the confirmation to the write.
        assert outcome.intent is not None
        result = await _apply_with_services(
            outcome.intent, owner_id, services, change_id=outcome.change_id
        )
        # If the write failed, re-stage the pending so the owner can retry
        # with another YES instead of having to resend the whole request.
        try:
            import json as _json
            body = _json.loads(result.body)
            if body.get("status") == "failed":
                await services.pending.stage_pending(outcome.intent)
        except Exception:
            pass  # Best effort — if re-staging fails, owner must resend
        return result

    async def _apply_with_services(
        intent: dict[str, Any],
        owner_id: str,
        services: TrackBServices,
        *,
        change_id: str | None = None,
    ) -> JSONResponse:
        # Multi-site: resolve by site_id when present, else fall back to first site.
        target_site_id = intent.get("site_id")
        if target_site_id:
            site = services.sites.get_site(target_site_id)
            if site is None or site.owner_id != owner_id:
                return _respond(
                    _result(
                        "failed",
                        change_id or f"ch-{uuid.uuid4().hex[:12]}",
                        error_message=f"site {target_site_id!r} not found for this owner",
                    )
                )
        else:
            sites = services.sites.sites_for_owner(owner_id)
            if not sites:
                return _respond(
                    _result(
                        "failed",
                        change_id or f"ch-{uuid.uuid4().hex[:12]}",
                        error_message="no onboarded site for this owner — complete onboarding first",
                    )
                )
            site = sites[0]
        try:
            client = services.make_client(site)
        except Exception as exc:
            logger.error("could not build WordPress client for %s: %s", site.site_id, exc)
            return _respond(
                _result(
                    "failed",
                    change_id or f"ch-{uuid.uuid4().hex[:12]}",
                    error_message="site credentials are unavailable",
                )
            )
        result = await apply_intent(
            intent, site.allowlist, client, services.changelog, change_id=change_id
        )
        return _respond(result)

    # ------------------------------------------------------- sites list

    @app.get("/sites/list", response_model=None)
    async def list_sites(owner_id: str) -> dict[str, Any]:
        """List all sites onboarded for this owner (multi-site support)."""
        services = await get_services()
        sites = services.sites.sites_for_owner(owner_id)
        # Find which site is active (has active_site_id set) in one query.
        active_id = None
        if sites:
            with services.sites._connect() as conn:
                row = conn.execute(
                    "SELECT site_id FROM onboarded_sites WHERE owner_id = ? AND active_site_id IS NOT NULL",
                    (owner_id,),
                ).fetchone()
                if row:
                    active_id = row["site_id"]
        return {
            "sites": [
                {
                    "site_id": s.site_id,
                    "site_url": s.site_url,
                    "status": s.status,
                    "is_active": s.site_id == active_id,
                }
                for s in sites
            ]
        }

    @app.post("/sites/{site_id}/active", response_model=None)
    async def set_active_site(site_id: str, owner_id: str) -> dict[str, str]:
        """Mark a site as the owner's active site (multi-site support)."""
        services = await get_services()
        site = services.sites.get_site(site_id)
        if site is None or site.owner_id != owner_id:
            raise HTTPException(status_code=404, detail="site not found for this owner")
        services.sites.set_active_site(owner_id, site_id)
        return {"status": "ok", "active_site_id": site_id}

    # ------------------------------------------------------- undo

    @app.post("/undo", response_model=None)
    async def undo_endpoint(payload: UndoRequest) -> JSONResponse:
        """Reverse-apply the owner's most recent change (B4)."""
        services = await get_services()
        # Multi-site: resolve by site_id when present, else fall back to first site.
        if payload.site_id:
            site = services.sites.get_site(payload.site_id)
            if site is None or site.owner_id != payload.owner_id:
                return _respond(
                    _result(
                        "failed",
                        f"ch-{uuid.uuid4().hex[:12]}",
                        error_message=f"site {payload.site_id!r} not found for this owner",
                    )
                )
        else:
            sites = services.sites.sites_for_owner(payload.owner_id)
            if not sites:
                return _respond(
                    _result(
                        "failed",
                        f"ch-{uuid.uuid4().hex[:12]}",
                        error_message="no onboarded site for this owner",
                    )
                )
            site = sites[0]
        client = services.make_client(site)
        outcome = await undo(
            payload.owner_id, client, services.changelog,
            site_id=payload.site_id, window=UNDO_WINDOW_SECONDS
        )
        if outcome.status == "undone":
            return _respond(
                _result(
                    "success",
                    outcome.change_id or f"ch-{uuid.uuid4().hex[:12]}",
                    before=outcome.before,
                    after=outcome.after,
                    live_url=outcome.live_url,
                )
            )
        return _respond(
            _result(
                "failed",
                outcome.original_change_id or f"ch-{uuid.uuid4().hex[:12]}",
                error_message=outcome.message,
            )
        )

    @app.get("/changes", response_model=None)
    async def list_changes_endpoint(owner_id: str, limit: int = 5) -> JSONResponse:
        """§10 Recap: list recent changes for the owner (last N entries)."""
        services = await get_services()
        changes = await services.changelog.list_changes(
            owner_id=owner_id,
            limit=limit,
        )
        return JSONResponse({
            "changes": [
                {
                    "change_id": c.change_id,
                    "action": c.action,
                    "content_type": c.content_type,
                    "before": c.before,
                    "after": c.after,
                    "live_url": c.live_url,
                    "created_at": c.created_at.isoformat() if c.created_at else None,
                }
                for c in changes
            ]
        })

    return app


app = create_app()
