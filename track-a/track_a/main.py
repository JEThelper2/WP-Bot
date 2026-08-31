"""WP-Bot Track A — WhatsApp Cloud API inbound webhook receiver.

Current scope:
- GET  /webhook  — Meta's verification handshake (hub.mode / hub.verify_token / hub.challenge).
- POST /webhook  — accepts inbound WhatsApp messages (text and voice-note
  media), extracts the owner phone, message type, raw content / media
  reference, and timestamp, and persists each one to the message log.
- Runs each new message through the inbound pipeline: text becomes
  `message_text` directly; voice notes are downloaded via Meta's Media
  API and transcribed (Whisper), routing to a low-confidence fallback
  reply when the transcript can't be trusted. See `track_a.pipeline`.
- A3/A4/A5 + the Integration Phase build the rest of the conversation:
  intent parsing, the confirm/clarify/escalate branches, the full outbound
  flow (confirmation -> YES/NO -> real Track B -> completion/error reply),
  and the UNDO command. The webhook now drives the whole thing: after a
  message is logged and normalized, its `message_text` is fed to the
  router (`app.state.router`, `track_a.routing.IntentRouter`), which
  stages confirmations at Track B (B3), relays YES/NO, and sends every
  outbound reply through the sender.

Meta expects every accepted webhook delivery to be answered with HTTP 200;
anything else makes Meta retry (or drop) the delivery. When
`WHATSAPP_APP_SECRET` is configured, deliveries are verified against
`X-Hub-Signature-256` (HMAC-SHA256 of the raw body) before anything is
parsed or logged — forged payloads are rejected with 403.
"""

from __future__ import annotations

import hmac as _hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from shared_contract.logging import setup_logging

from .admin import router as admin_router
from .ai_provider import get_provider
from .config import DEFAULT_VERIFY_TOKEN, Settings
from .dashboard import router as dashboard_router
from .login import require_admin, router as login_router
from .intent import IntentParser
from .media import WhatsAppMediaClient
from .metrics import metrics
from .onboarding import OnboardingFlow
from .pipeline import MessageProcessor
from .ratelimit import RateLimiter
from .reply import ReplySender, WhatsAppReplySender
from .reliability import ReliabilityLayer
from .telegram import TelegramReplySender, handle_telegram_update
from .routing import IntentRouter
from .signature import verify_webhook_signature
from .store import (
    count_escalation_requests,
    count_messages,
    get_message,
    init_db,
    insert_message,
    list_escalation_requests,
    list_messages,
    log_escalation_request,
)
from .tenant_store import get_tenant_by_sender, init_tenant_db
from .trackb import TrackBClient
from .transcribe import get_transcription_provider

logger = logging.getLogger("track_a.webhook")

# Meta's Cloud API webhook object type for WhatsApp business accounts.
_WABA_OBJECT = "whatsapp_business_account"

# Media-ish message types whose payload we keep as a JSON media reference.
# (voice notes arrive as type "audio" with a `voice: true` flag)
_MEDIA_TYPES = {"audio", "image", "video", "sticker", "document"}


def create_app(
    settings: Settings | None = None,
    processor: MessageProcessor | None = None,
    router: IntentRouter | None = None,
) -> FastAPI:
    setup_logging()
    settings = settings or Settings.from_env()
    init_db(settings.db_path)
    init_tenant_db(settings.db_path)  # §1: multi-tenant tables

    # Shared httpx.AsyncClient — lifecycle managed by the FastAPI lifespan.
    shared_client = httpx.AsyncClient()

    sender = WhatsAppReplySender(
        api_token=settings.api_token,
        phone_number_id=settings.phone_number_id,
        api_version=settings.api_version,
        client=shared_client,
    )
    if processor is None:
        # Wire the transcription provider from config (Dependency Inversion):
        # the pipeline never knows which backend is in use.
        transcriber = get_transcription_provider(
            settings.transcription_provider,
        )
        processor = MessageProcessor(
            db_path=settings.db_path,
            media_client=WhatsAppMediaClient(
                api_token=settings.api_token,
                api_version=settings.api_version,
                client=shared_client,
            ),
            transcriber=transcriber,
            sender=sender,
        )

    # §6: DB-backed reliability layer (idempotency + rate limiting + circuit breaker).
    # Created early so the router can use it for circuit breaker on Track B calls.
    reliability = ReliabilityLayer(
        db_path=settings.db_path,
        max_messages=30,
        window_hours=1,
    )

    if router is None:
        trackb = TrackBClient(base_url=settings.track_b_url, client=shared_client)
        # Wire the AI provider from config (Dependency Inversion):
        # the router/parser never knows which backend is in use.
        provider_kwargs: dict[str, str] = {}
        if settings.ai_api_key:
            provider_kwargs["api_key"] = settings.ai_api_key
        if settings.ai_model:
            provider_kwargs["model"] = settings.ai_model
        llm = get_provider(
            settings.ai_provider,
            fallback_name=settings.ai_fallback_provider or None,
            **provider_kwargs,
        )
        parser = IntentParser(llm=llm)
        from .session import DBSessionStore

        router = IntentRouter(
            parser=parser,
            sender=sender,
            trackb=trackb,
            sessions=DBSessionStore(settings.db_path),
            onboarding=OnboardingFlow(trackb=trackb, db_path=settings.db_path),
            log_escalation=lambda owner, msg: log_escalation_request(settings.db_path, owner, msg),
            reliability=reliability,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: shared_client is already created above.
        yield
        # Shutdown: close the shared httpx.AsyncClient.
        await shared_client.aclose()

    app = FastAPI(
        title="WP-Bot Track A (WhatsApp conversation service)",
        version="0.1.0",
        description=(
            "Inbound WhatsApp Cloud API webhook receiver. Verifies Meta's "
            "handshake, logs inbound messages, and (later) turns them into "
            "intent objects for Track B."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.processor = processor
    app.state.router = router
    app.state.admin_token = settings.admin_token
    admin_user = os.environ.get("ADMIN_USERNAME", "")
    admin_pass = os.environ.get("ADMIN_PASSWORD", "")
    if not settings.admin_token and not (admin_user and admin_pass):
        logger.warning(
            "Neither ADMIN_TOKEN nor ADMIN_USERNAME/ADMIN_PASSWORD are set — "
            "admin dashboard and API are UNPROTECTED. Set credentials for production."
        )
    if settings.verify_token == DEFAULT_VERIFY_TOKEN:
        logger.warning(
            "WHATSAPP_VERIFY_TOKEN is using the default dev value — "
            "set a unique token in production to prevent webhook hijacking."
        )
    if not settings.app_secret:
        logger.warning(
            "WHATSAPP_APP_SECRET is not set — webhook signature verification "
            "is DISABLED. Set this in production to reject forged deliveries."
        )
    # Per-owner rate limiter: 30 messages per 60s window.
    app.state.rate_limiter = RateLimiter(max_requests=30, window_seconds=60)

    # §6: DB-backed reliability layer (idempotency + rate limiting + circuit breaker).
    app.state.reliability = reliability

    # --- Telegram adapter (for testing without WhatsApp) -----------------------
    tg_sender: TelegramReplySender | ReplySender | None = None
    if settings.telegram_bot_token:
        tg_sender = TelegramReplySender(
            bot_token=settings.telegram_bot_token,
            client=shared_client,
        )
        # If no WhatsApp credentials, use the Telegram sender as the
        # default reply channel so the pipeline works end-to-end.
        if not settings.api_token:
            sender = tg_sender
            # Re-build router with the Telegram sender so all replies
            # go through Telegram.
            router = IntentRouter(
                parser=router.parser,
                sender=sender,
                trackb=router.trackb,
                sessions=router.sessions,
                onboarding=router.onboarding,
                log_escalation=router.log_escalation,
                active_sites=router.active_sites,
            )
            app.state.router = router
        app.state.tg_sender = tg_sender
        logger.info("Telegram adapter enabled (bot token configured)")
    else:
        app.state.tg_sender = None

    # Internal admin views (PRD §10 + dashboard).
    # Dashboard must be registered first to avoid /admin/dashboard being
    # caught by admin's /admin/{escalation_id} catch-all route.
    app.include_router(login_router)
    app.include_router(dashboard_router)
    app.include_router(admin_router)

    # --- Static landing page (served from the site/ directory) -----------
    _site_dir = Path(__file__).resolve().parent.parent.parent / "site"
    if _site_dir.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/site", StaticFiles(directory=str(_site_dir), html=True), name="site")
        # Serve the root landing page at /

        @app.get("/")
        async def landing_page() -> Any:
            from starlette.responses import FileResponse

            return FileResponse(str(_site_dir / "index.html"))

    @app.get("/health")
    async def health(_admin: str = Depends(require_admin)) -> dict[str, str]:
        """Liveness + basic readiness check (SQLite reachable). Admin-only."""
        try:
            import sqlite3

            conn = sqlite3.connect(str(settings.db_path))
            conn.execute("SELECT 1")
            conn.close()
            db_ok = True
        except Exception:
            db_ok = False
        status = "ok" if db_ok else "degraded"
        return {"status": status, "service": "track-a", "db": "ok" if db_ok else "unreachable"}

    @app.get("/metrics")
    async def metrics_endpoint(_admin: str = Depends(require_admin)) -> PlainTextResponse:
        """Prometheus-style metrics in text exposition format. Admin-only."""
        return PlainTextResponse(metrics.render(), media_type="text/plain")

    # --- Telegram adapter endpoints (for testing without WhatsApp) ----------

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict[str, str]:
        """Receive Telegram Bot API updates.

        Set this URL as the Telegram webhook via BotFather's setWebhook
        command, or call it directly for manual testing:

            curl -X POST http://localhost:8000/telegram/webhook \
              -H 'Content-Type: application/json' \
              -d '{"message": {"chat": {"id": 123456}, "text": "hello"}}'
        """
        try:
            payload: Any = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        if not settings.telegram_bot_token:
            raise HTTPException(status_code=503, detail="Telegram not configured (set TELEGRAM_BOT_TOKEN)")

        # Validate the Telegram webhook secret token (set via BotFather's
        # setWebhook secret_token parameter).  When configured, requests
        # without a valid X-Telegram-Bot-Api-Secret-Token are rejected.
        if settings.telegram_webhook_secret:
            provided = request.headers.get("x-telegram-bot-api-secret-token", "")
            if not _hmac.compare_digest(provided, settings.telegram_webhook_secret):
                logger.warning("Telegram webhook rejected: invalid secret token")
                raise HTTPException(status_code=403, detail="Invalid secret token")

        return await handle_telegram_update(
            payload,
            router=app.state.router,
        )

    @app.get("/telegram/poll")
    async def telegram_poll(_admin: str = Depends(require_admin)) -> dict[str, Any]:
        """Long-poll Telegram for updates (dev mode). Admin-only."""
        if not settings.telegram_bot_token:
            raise HTTPException(status_code=503, detail="Telegram not configured (set TELEGRAM_BOT_TOKEN)")

        import httpx as _httpx

        async with _httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/getUpdates",
                params={"timeout": 5, "offset": -1},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()

        processed = 0
        for update in data.get("result", []):
            await handle_telegram_update(update, router=app.state.router)
            processed += 1

        return {"status": "ok", "processed": processed}

    @app.post("/telegram/send")
    async def telegram_send(chat_id: str, text: str, _admin: str = Depends(require_admin)) -> dict[str, str]:
        """Manual send endpoint for testing. Admin-only."""
        if not settings.telegram_bot_token:
            raise HTTPException(status_code=503, detail="Telegram not configured")
        sender = TelegramReplySender(bot_token=settings.telegram_bot_token, client=shared_client)
        await sender.send(chat_id, text)
        return {"status": "ok"}

    # --- WhatsApp webhook ---------------------------------------------------

    @app.get("/webhook")
    async def verify_webhook(
        mode: str | None = Query(default=None, alias="hub.mode"),
        token: str | None = Query(default=None, alias="hub.verify_token"),
        challenge: str | None = Query(default=None, alias="hub.challenge"),
    ) -> PlainTextResponse:
        """Meta's subscription verification handshake.

        Meta GETs this URL with `hub.mode=subscribe`, `hub.verify_token`,
        and `hub.challenge` when we subscribe the webhook. We must echo
        `hub.challenge` back to prove we own the endpoint.
        """
        if mode == "subscribe" and token == settings.verify_token and challenge:
            logger.info("webhook verification handshake accepted")
            return PlainTextResponse(challenge)
        logger.warning(
            "webhook verification failed: mode=%r token_match=%s",
            mode,
            token == settings.verify_token,
        )
        raise HTTPException(status_code=403, detail="Webhook verification failed")

    @app.post("/webhook")
    async def webhook(request: Request) -> dict[str, Any]:
        """Receive, verify, and log inbound WhatsApp messages.

        When `WHATSAPP_APP_SECRET` is configured, every delivery must carry
        a valid `X-Hub-Signature-256` (HMAC-SHA256 of the RAW body with the
        app secret). Verification runs before anything is parsed or logged,
        so a forged delivery can never reach the message log or the
        conversation.
        """
        raw_body = await request.body()
        if settings.app_secret:
            signature = request.headers.get("x-hub-signature-256")
            if not verify_webhook_signature(settings.app_secret, raw_body, signature):
                logger.warning(
                    "webhook delivery rejected: X-Hub-Signature-256 invalid "
                    "(forged or misconfigured secret?)"
                )
                raise HTTPException(status_code=403, detail="Webhook signature verification failed")

        try:
            payload: Any = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

        if not isinstance(payload, dict) or payload.get("object") != _WABA_OBJECT:
            # Meta: answer unrecognized objects with 404 so Meta stops
            # retrying them against this endpoint.
            raise HTTPException(
                status_code=404,
                detail=f"Unrecognized object: expected {_WABA_OBJECT!r}",
            )

        reliability = app.state.reliability
        received = 0
        duplicates = 0
        rate_limited = 0
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # `statuses` entries are delivery receipts — not messages,
                # nothing to log, and we must still answer 200.
                for msg in value.get("messages", []):
                    owner_phone = str(msg.get("from") or "")
                    provider_msg_id = str(msg.get("id") or "")

                    # §5: Resolve tenant_id from sender_id.  If no tenant
                    # record exists, fall back to legacy single-owner mode
                    # (the old in-memory rate limiter + wam_id uniqueness).
                    tenant = get_tenant_by_sender(settings.db_path, owner_phone) if owner_phone else None
                    tenant_id = tenant["id"] if tenant else None

                    if tenant_id is not None:
                        # §6.1: Idempotency — checked BEFORE any AI call.
                        if not reliability.check_idempotency(tenant_id, provider_msg_id):
                            duplicates += 1
                            metrics.inc("messages_duplicate")
                            continue

                        # §6.2: Rate limiting — DB-backed per-tenant.
                        is_limited, _count = reliability.check_rate_limit(tenant_id)
                        if is_limited:
                            rate_limited += 1
                            metrics.inc("messages_rate_limited")
                            logger.warning(
                                "rate-limited message from tenant %s (owner %s)",
                                tenant_id, owner_phone,
                            )
                            continue
                    else:
                        # Legacy path: no tenant record yet (pre-onboarding).
                        # Use the in-memory rate limiter + wam_id uniqueness.
                        if owner_phone and app.state.rate_limiter.is_rate_limited(owner_phone):
                            rate_limited += 1
                            metrics.inc("messages_rate_limited")
                            logger.warning(
                                "rate-limited message from owner %s", owner_phone
                            )
                            continue

                    row_id = _log_message(settings.db_path, msg)
                    if row_id is None:
                        duplicates += 1
                        metrics.inc("messages_duplicate")
                        continue
                    received += 1
                    metrics.inc("messages_received")
                    # Run the inbound pipeline (text normalize / voice
                    # transcription). Blocking transcription belongs on a
                    # queue in production; fine inline at this stage.
                    await app.state.processor.process_row(row_id)
                    # Then the conversation: drive the intent router from the
                    # normalized message_text (text messages and successful
                    # voice transcripts).
                    await _route_message(app, settings.db_path, row_id)

        logger.info(
            "webhook delivery: %d new message(s), %d duplicate(s), %d rate-limited",
            received,
            duplicates,
            rate_limited,
        )
        return {
            "status": "ok",
            "received": received,
            "duplicates": duplicates,
            "rate_limited": rate_limited,
        }

    @app.get("/messages")
    async def messages(_admin: str = Depends(require_admin)) -> dict[str, Any]:
        """Recent logged messages (newest first). Admin-only."""
        return {
            "count": count_messages(settings.db_path),
            "messages": list_messages(settings.db_path),
        }

    @app.get("/escalations")
    async def escalations(_admin: str = Depends(require_admin)) -> dict[str, Any]:
        """Escalation queue (PRD §10). Admin-only."""
        return {
            "count": count_escalation_requests(settings.db_path),
            "escalations": list_escalation_requests(settings.db_path),
        }

    return app


async def _route_message(app: FastAPI, db_path: Any, row_id: int) -> None:
    """Feed one processed inbound message to the conversation router.

    Rows without `message_text` (unsupported types, low-confidence voice
    notes) stop at the pipeline and never reach the router. A router
    failure must never break the HTTP 200 Meta needs (otherwise Meta
    retries the delivery) — log and move on.
    """
    row = get_message(db_path, row_id)
    message_text = (row or {}).get("message_text")
    if not message_text:
        return
    # §4.1: voice notes get source="voice" to trigger the echo-back flow.
    source = "voice" if (row or {}).get("message_type") == "audio" else "text"
    try:
        await app.state.router.handle_message(
            row["owner_phone"], message_text, source=source
        )
    except Exception:
        logger.exception("router failed for message row %s", row_id)


def _log_message(db_path: Any, msg: dict[str, Any]) -> int | None:
    """Extract the fields we care about and persist one message.

    Returns the new row id, or None for duplicate deliveries (Meta
    redelivers on retries).
    """
    message_type = str(msg.get("type", "unknown"))
    if message_type == "text":
        content: str | None = (msg.get("text") or {}).get("body")
        media_ref = None
    elif message_type in _MEDIA_TYPES:
        media = msg.get(message_type) or {}
        media_ref = media.get("id")
        content = _content_json(media)
    else:
        # Unknown/other types: keep the raw message as content so nothing
        # is silently dropped.
        content = _content_json(msg)
        media_ref = None

    return insert_message(
        db_path,
        wam_id=str(msg.get("id") or ""),
        owner_phone=str(msg.get("from") or ""),
        message_type=message_type,
        content=content,
        media_ref=media_ref,
        meta_timestamp=str(msg.get("timestamp")),
    )


def _content_json(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


# Module-level app instance for ASGI servers (uvicorn track_a.main:app).
# The app is created lazily on first attribute access so that importing
# this module (e.g. from tests) does not trigger side effects like
# database initialization or HTTP client creation.
_app_instance: FastAPI | None = None


def _get_app() -> FastAPI:
    global _app_instance  # noqa: PLW0603
    if _app_instance is None:
        _app_instance = create_app()
    return _app_instance


class _AppProxy:
    """Lazy proxy so `uvicorn track_a.main:app` works without eager init."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_get_app(), name)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        return await _get_app()(scope, receive, send)


app: FastAPI = _AppProxy()  # type: ignore[assignment]
