"""Allowlist and validation gate (B2) between intent objects and the
WordPress client (B1).

PRD guardrail: **no write may ever touch anything outside the site's
allowlist, regardless of what the intent object claims.** Every intent
goes through `apply_intent`, which:

1. validates the intent against `shared-contract/intent.schema.json`
   with `validate_intent()` — Track A's validation is never trusted;
2. checks the site config: the content_type must be enabled, and every
   field in the intent must be in that content type's field mapping;
3. only then dispatches to the WordPress client.

Any rejection produces a contract-valid result object with
`status: "failed"` and a clear `error_message` — never an exception
escaping to the caller, and never a WordPress call.

The pilot/demo site config is hardcoded here; `get_site_config` is the
registry seam that will become DB/per-site config later.
"""

from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from shared_contract import CONTRACT_VERSION, ContractValidationError, validate_intent, validate_result

from .changelog import ChangeLog, ChangeRow
from .secrets import redact
from .wordpress import WordPressClient, WordPressError

logger = logging.getLogger("track_b.allowlist")

# Content types the product supports. `image` is v1.5 / optional for the
# v1 MVP — sites opt in by enabling it in their config.
CONTENT_TYPES = ("job", "announcement", "business_info", "image")


@dataclass(frozen=True)
class ContentTypeMapping:
    content_type: str
    enabled: bool = True
    allowed_fields: tuple[str, ...] = ()
    # WordPress mapping (informational for now — B1 implements it):
    post_type: str | None = None  # "posts" | "jobs" (custom post type)
    category: str | None = None  # standard-post fallback category
    option_key: str | None = None  # business_info singleton option
    image_slots: tuple[str, ...] = ()  # theme slots allowed for image (v1.5)


@dataclass(frozen=True)
class SiteConfig:
    site_url: str
    mappings: dict[str, ContentTypeMapping] = field(default_factory=dict)

    def mapping_for(self, content_type: str) -> ContentTypeMapping | None:
        return self.mappings.get(content_type)


# ---------------------------------------------------------------------------
# Pilot / demo site configuration. Structured so it can become per-site
# config (DB/JSON) later without touching the validation logic.
# ---------------------------------------------------------------------------

PILOT_SITE_CONFIG = SiteConfig(
    site_url="https://example.com",
    mappings={
        "job": ContentTypeMapping(
            content_type="job",
            post_type="posts",  # B1 auto-upgrades to a "jobs" CPT if present
            category="jobs",
            allowed_fields=("title", "description", "location", "remote", "category"),
        ),
        "announcement": ContentTypeMapping(
            content_type="announcement",
            post_type="posts",
            category="announcements",
            allowed_fields=("title", "body", "expires_at"),
        ),
        "business_info": ContentTypeMapping(
            content_type="business_info",
            option_key="wpbot_business_info",
            allowed_fields=("phone", "hours", "address", "prices"),
        ),
        "image": ContentTypeMapping(
            content_type="image",
            enabled=False,  # v1.5 — off for the pilot MVP
            image_slots=("homepage_banner", "logo", "gallery"),
            allowed_fields=("slot", "media_url", "media_base64"),
        ),
    },
)

_SITE_CONFIGS: dict[str, SiteConfig] = {
    PILOT_SITE_CONFIG.site_url: PILOT_SITE_CONFIG,
}


class UnknownSiteError(Exception):
    def __init__(self, site_url: str) -> None:
        super().__init__(f"no site configuration exists for {site_url!r}")
        self.site_url = site_url


class IntentNotAllowedError(Exception):
    """The intent violates the site's allowlist (clear, safe message)."""


def get_site_config(site_url: str) -> SiteConfig:
    """Resolve a site's config — the seam for per-site config later."""
    config = _SITE_CONFIGS.get(site_url)
    if config is None:
        raise UnknownSiteError(site_url)
    return config


def config_to_dict(config: SiteConfig) -> dict[str, Any]:
    """Serialize a SiteConfig (persisted at onboarding; per-site later)."""
    return {
        "site_url": config.site_url,
        "mappings": {
            ct: {
                "content_type": m.content_type,
                "enabled": m.enabled,
                "allowed_fields": list(m.allowed_fields),
                "post_type": m.post_type,
                "category": m.category,
                "option_key": m.option_key,
                "image_slots": list(m.image_slots),
            }
            for ct, m in config.mappings.items()
        },
    }


def config_from_dict(data: dict[str, Any]) -> SiteConfig:
    """Rebuild a SiteConfig from `config_to_dict` output."""
    return SiteConfig(
        site_url=data["site_url"],
        mappings={
            ct: ContentTypeMapping(
                content_type=m.get("content_type", ct),
                enabled=bool(m.get("enabled", True)),
                allowed_fields=tuple(m.get("allowed_fields", ())),
                post_type=m.get("post_type"),
                category=m.get("category"),
                option_key=m.get("option_key"),
                image_slots=tuple(m.get("image_slots", ())),
            )
            for ct, m in data.get("mappings", {}).items()
        },
    )


def register_site_config(config: SiteConfig) -> None:
    """Register/replace a site config (used by tests and future provisioning)."""
    _SITE_CONFIGS[config.site_url] = config


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_intent_for_site(intent: dict[str, Any], config: SiteConfig) -> None:
    """Schema + allowlist validation. Raises on the first violation.

    Order matters: schema first (never trust Track A), then content type
    enablement, then field allowlist.
    """
    # 1. Contract schema — a malformed intent is rejected before anything
    #    else, regardless of what it claims.
    validate_intent(intent)

    # 2. Content type enabled for this site?
    content_type = intent["content_type"]
    mapping = config.mapping_for(content_type)
    if mapping is None or not mapping.enabled:
        raise IntentNotAllowedError(
            f"content_type {content_type!r} is not enabled for this site"
        )

    # 3. Every field must be in the site's mapping for that content type.
    unknown = [f for f in intent["fields"] if f not in mapping.allowed_fields]
    if unknown:
        raise IntentNotAllowedError(
            f"field(s) not allowed for content_type {content_type!r} on this "
            f"site: {', '.join(sorted(unknown))}"
        )


# ---------------------------------------------------------------------------
# Apply (validate -> dispatch -> contract-valid result)
# ---------------------------------------------------------------------------


def _failed_result(error_message: str) -> dict[str, Any]:
    result = {
        "contract_version": CONTRACT_VERSION,
        "status": "failed",
        "change_id": f"ch-{uuid.uuid4().hex[:12]}",
        "before": None,
        "after": None,
        "live_url": None,
        "error_message": redact(error_message),
    }
    validate_result(result)  # boundary discipline: what we emit is valid
    return result


async def apply_intent(
    intent: dict[str, Any],
    config: SiteConfig,
    client: WordPressClient,
    changelog: ChangeLog | None = None,
) -> dict[str, Any]:
    """Validate an intent against the site's allowlist and apply it.

    Returns a contract-valid result object. A gate violation or a client
    error becomes `status: "failed"` with a clear error_message — no
    exception escapes, and no WordPress write happens before validation.

    PRD §11: when a `changelog` is provided, every successful write is
    logged in the same flow — a write that cannot be logged is treated as
    a failure (undo depends entirely on this log), and the result's
    change_id is the log row's change_id.
    """
    try:
        validate_intent_for_site(intent, config)
    except (ContractValidationError, IntentNotAllowedError) as exc:
        logger.info("intent rejected by gate: %s", exc)
        return _failed_result(str(exc))

    change_id = f"ch-{uuid.uuid4().hex[:12]}"
    try:
        record = await _dispatch(intent, client)
    except (WordPressError, IntentNotAllowedError) as exc:
        logger.warning(
            "WordPress write failed for owner %s: %s", intent["owner_id"], exc
        )
        return _failed_result(str(exc))

    if changelog is not None:
        try:
            await changelog.record_change(
                ChangeRow(
                    change_id=change_id,
                    owner_id=intent["owner_id"],
                    content_type=intent["content_type"],
                    action=intent["action"],
                    before=record.before,
                    after=record.after,
                    live_url=record.live_url,
                )
            )
        except Exception as exc:
            logger.error(
                "change %s was applied but could not be logged: %s",
                change_id,
                exc,
            )
            return _failed_result(
                "the change was applied but could not be logged — treat as "
                "failed; undo is unavailable for it"
            )

    result = {
        "contract_version": CONTRACT_VERSION,
        "status": "success",
        "change_id": change_id,
        "before": record.before,
        "after": record.after,
        "live_url": record.live_url,
        "error_message": None,
    }
    validate_result(result)
    return result


async def _dispatch(intent: dict[str, Any], client: WordPressClient) -> Any:
    """Route a validated intent to the right WordPressClient operation."""
    content_type = intent["content_type"]
    action = intent["action"]
    fields = intent["fields"]

    if content_type in ("job", "announcement"):
        if action == "create":
            return await client.create_post(content_type, fields)
        # update/delete identify the post by its title (the contract has no
        # post id field, so title is the stable handle for v1).
        title = fields.get("title")
        if not title:
            raise IntentNotAllowedError(
                f"{content_type} {action} requires a title to identify the post"
            )
        post_id = await client.find_post_by_title(content_type, str(title))
        if post_id is None:
            raise WordPressError(
                f"no {content_type} posting titled {title!r} was found to {action}"
            )
        if action == "update":
            return await client.update_post(post_id, fields, content_type=content_type)
        return await client.delete_post(post_id, content_type=content_type)

    if content_type == "business_info":
        return await client.update_site_option(fields)

    if content_type == "image":
        slot = fields.get("slot")
        if slot is None:
            raise IntentNotAllowedError("image intent requires a slot")
        media: dict[str, Any] = {}
        if fields.get("media_base64"):
            try:
                media["content"] = base64.b64decode(fields["media_base64"])
            except (ValueError, TypeError) as exc:
                raise IntentNotAllowedError("media_base64 is not valid base64") from exc
            media["filename"] = f"wpbot-{slot}.img"
            media["mime_type"] = "application/octet-stream"
        elif fields.get("media_url"):
            raise IntentNotAllowedError(
                "image intents with media_url are not supported yet — send "
                "media_base64 instead (v1.5)"
            )
        else:
            raise IntentNotAllowedError("image intent requires media_base64 or media_url")
        return await client.upload_and_replace_image(slot, media)

    raise IntentNotAllowedError(
        f"content_type {content_type!r} is not supported"
    )
