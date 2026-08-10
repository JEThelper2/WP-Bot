"""Onboarding validation flow (PRD §12, step 3).

`onboard_site()` takes a site URL + username + application password,
validates them against the live site, and — on success — persists an
onboarded site record (site_id, owner_id, encrypted credentials, the
default B2 allowlist config, status `active`).

Validation distinguishes the failure modes Track A's onboarding
conversation needs to explain to a non-technical owner:

- `invalid_url`             — the URL isn't a plausible http(s) site;
- `unreachable`             — the site can't be reached;
- `not_wordpress`           — reachable, but no WordPress REST API there;
- `invalid_credentials`     — the application password was rejected;
- `insufficient_permissions`— valid credentials, but the user lacks
  `edit_posts` (e.g. a Subscriber, not Editor+).

The probe is **read-only** (fetch the authenticated user via
`users/me` and check capabilities). A test write-then-undo would prove
edit access with stronger confidence but mutates the site during
onboarding; for v1 the capability check is the tradeoff we accept
(document in the summary). Credentials are encrypted with the Fernet
Vault and never logged; a failed onboarding persists nothing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .allowlist import PILOT_SITE_CONFIG, SiteConfig, config_to_dict, config_from_dict
from .secrets import Vault
from .wordpress import WordPressClient, WordPressError

logger = logging.getLogger("track_b.onboarding")

# Validation reasons, in the order they are checked.
REASON_OK = "ok"
REASON_INVALID_URL = "invalid_url"
REASON_UNREACHABLE = "unreachable"
REASON_NOT_WORDPRESS = "not_wordpress"
REASON_INVALID_CREDENTIALS = "invalid_credentials"
REASON_INSUFFICIENT_PERMISSIONS = "insufficient_permissions"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS onboarded_sites (
    site_id           TEXT PRIMARY KEY,
    owner_id          TEXT NOT NULL,
    site_url          TEXT NOT NULL UNIQUE,
    username          TEXT NOT NULL,
    encrypted_password TEXT NOT NULL,
    allowlist_config  TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str
    message: str = ""
    roles: tuple[str, ...] = ()
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OnboardedSite:
    site_id: str
    owner_id: str
    site_url: str
    username: str
    status: str
    allowlist: SiteConfig
    encrypted_password: str | None = None  # only surfaced internally


@dataclass(frozen=True)
class OnboardResult:
    status: str  # "success" | "failed"
    reason: str
    message: str = ""
    site_id: str | None = None
    site_url: str | None = None


class OnboardedSiteStore:
    """SQLite store of onboarded sites (application passwords ciphertext)."""

    def __init__(self, db_path: Path, vault: Vault | None = None) -> None:
        self.db_path = db_path
        self.vault = vault or Vault()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def add_site(
        self,
        *,
        owner_id: str,
        site_url: str,
        username: str,
        app_password: str,
        allowlist: SiteConfig,
    ) -> OnboardedSite:
        """Insert, or refresh an existing site with the same URL."""
        token = self.vault.encrypt(app_password)
        now = datetime.now(timezone.utc).isoformat()
        config_json = json.dumps(config_to_dict(allowlist))
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT site_id FROM onboarded_sites WHERE site_url = ?",
                (site_url,),
            ).fetchone()
            if existing is not None:
                site_id = existing[0]
                conn.execute(
                    """
                    UPDATE onboarded_sites SET owner_id = ?, username = ?,
                        encrypted_password = ?, allowlist_config = ?,
                        status = 'active'
                    WHERE site_url = ?
                    """,
                    (owner_id, username, token, config_json, site_url),
                )
            else:
                site_id = f"site-{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO onboarded_sites
                        (site_id, owner_id, site_url, username,
                         encrypted_password, allowlist_config, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (site_id, owner_id, site_url, username, token, config_json, now),
                )
        return OnboardedSite(
            site_id=site_id,
            owner_id=owner_id,
            site_url=site_url,
            username=username,
            status="active",
            allowlist=allowlist,
        )

    def get_site(self, site_id: str) -> OnboardedSite | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM onboarded_sites WHERE site_id = ?", (site_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_site(row)

    def get_site_by_url(self, site_url: str) -> OnboardedSite | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM onboarded_sites WHERE site_url = ?", (site_url,)
            ).fetchone()
        return self._row_to_site(row) if row else None

    def sites_for_owner(self, owner_id: str) -> list[OnboardedSite]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM onboarded_sites WHERE owner_id = ?", (owner_id,)
            ).fetchall()
        return [self._row_to_site(row) for row in rows]

    def credentials_for(self, site_id: str) -> tuple[str, str] | None:
        """(username, decrypted app password) for a site, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT username, encrypted_password FROM onboarded_sites "
                "WHERE site_id = ?",
                (site_id,),
            ).fetchone()
        if row is None:
            return None
        return row["username"], self.vault.decrypt(row["encrypted_password"])

    def set_status(self, site_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE onboarded_sites SET status = ? WHERE site_id = ?",
                (status, site_id),
            )

    def _row_to_site(self, row: sqlite3.Row) -> OnboardedSite:
        return OnboardedSite(
            site_id=row["site_id"],
            owner_id=row["owner_id"],
            site_url=row["site_url"],
            username=row["username"],
            status=row["status"],
            allowlist=config_from_dict(json.loads(row["allowlist_config"])),
            encrypted_password=row["encrypted_password"],
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _normalize_url(raw: str) -> str | None:
    """Return a normalized http(s) URL, or None if the input is invalid."""
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.split("@")[-1].split(":")[0]
    if not host or "." not in host and host not in ("localhost",):
        return None
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def has_edit_permissions(user: dict[str, Any]) -> bool:
    caps = user.get("capabilities") or {}
    return bool(caps.get("edit_posts"))


async def validate_site_access(
    site_url: str,
    username: str,
    app_password: str,
    *,
    client: WordPressClient | None = None,
) -> ValidationResult:
    """Read-only probe of credentials + permissions against the live site."""
    normalized = _normalize_url(site_url)
    if normalized is None:
        return ValidationResult(
            False, REASON_INVALID_URL, "the site URL is not a valid http(s) address"
        )

    wp = client or WordPressClient(normalized, username, app_password)
    try:
        user = await wp.get_current_user()
    except WordPressError as exc:
        if exc.status_code in (401, 403):
            return ValidationResult(
                False,
                REASON_INVALID_CREDENTIALS,
                "the application password was rejected by the site",
            )
        if exc.status_code == 404:
            return ValidationResult(
                False,
                REASON_NOT_WORDPRESS,
                "no WordPress REST API was found at this URL",
            )
        return ValidationResult(
            False, REASON_UNREACHABLE, f"could not reach the site ({exc})"
        )

    roles = tuple(user.get("roles") or ())
    caps = user.get("capabilities") or {}
    if not has_edit_permissions(user):
        return ValidationResult(
            False,
            REASON_INSUFFICIENT_PERMISSIONS,
            "the credentials are valid but the user lacks editing rights — "
            "the WordPress user needs at least the Editor role",
            roles=roles,
            capabilities=caps,
        )
    return ValidationResult(True, REASON_OK, "credentials valid", roles=roles, capabilities=caps)


# ---------------------------------------------------------------------------
# Onboard
# ---------------------------------------------------------------------------


async def onboard_site(
    site_url: str,
    username: str,
    app_password: str,
    owner_id: str,
    *,
    store: OnboardedSiteStore,
    client: WordPressClient | None = None,
) -> OnboardResult:
    """Validate, then persist the site record on success."""
    validation = await validate_site_access(
        site_url, username, app_password, client=client
    )
    if not validation.ok:
        logger.info(
            "onboarding rejected for %r: %s", site_url, validation.reason
        )
        return OnboardResult(
            status="failed",
            reason=validation.reason,
            message=validation.message,
        )

    normalized = _normalize_url(site_url)
    assert normalized is not None
    site = store.add_site(
        owner_id=owner_id,
        site_url=normalized,
        username=username,
        app_password=app_password,
        allowlist=PILOT_SITE_CONFIG,  # default B2 allowlist for new sites
    )
    logger.info("onboarded site %s for owner %s", site.site_id, owner_id)
    return OnboardResult(
        status="success",
        reason=REASON_OK,
        message="site onboarded",
        site_id=site.site_id,
        site_url=normalized,
    )
