"""Track B configuration.

Values come from the environment. The critical one is `WPBOT_SECRETS_KEY`
(see `track_b.secrets`): without it, stored WordPress application
passwords are encrypted with a per-process dev key and become
undecryptable after restart. Production must set it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "trackb.db"

# The single WordPress option that holds the business_info singleton when
# the site doesn't expose an ACF options page (see the recommended
# mu-plugin in wp-sandbox/).
BUSINESS_INFO_OPTION_KEY = "wpbot_business_info"

# v1.5: which theme slots upload_and_replace_image may write, and where the
# new attachment URL goes on the site (B2 expands this into full per-theme
# allowlists). Values are option keys inside the business_info singleton
# that the bundled mu-plugin explicitly allows (image:<slot>).
IMAGE_SLOT_ALLOWLIST: dict[str, str] = {
    "homepage_banner": "image:homepage_banner",
    "logo": "image:logo",
    "gallery": "image:gallery",
}


@dataclass(frozen=True)
class Settings:
    secrets_key: str = ""
    db_path: Path = _DEFAULT_DB
    redis_url: str = "redis://localhost:6379/0"
    pg_dsn: str = ""  # Postgres DSN for the change log; empty -> in-memory dev log
    business_info_option_key: str = BUSINESS_INFO_OPTION_KEY
    image_slot_allowlist: dict[str, str] = field(
        default_factory=lambda: dict(IMAGE_SLOT_ALLOWLIST)
    )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            secrets_key=os.environ.get("WPBOT_SECRETS_KEY", ""),
            db_path=Path(
                os.environ.get("WPBOT_TRACK_B_DB", str(_DEFAULT_DB))
            ),
            redis_url=os.environ.get(
                "WPBOT_REDIS_URL", "redis://localhost:6379/0"
            ),
            pg_dsn=os.environ.get("WPBOT_PG_DSN", ""),
            business_info_option_key=os.environ.get(
                "WPBOT_BUSINESS_INFO_OPTION", BUSINESS_INFO_OPTION_KEY
            ),
            image_slot_allowlist=IMAGE_SLOT_ALLOWLIST,
        )
