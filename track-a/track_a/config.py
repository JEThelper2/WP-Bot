"""Track A configuration.

Values come from the environment so the same code runs in dev and
production. Defaults are dev-friendly; production deployments should
set WHATSAPP_VERIFY_TOKEN and WHATSAPP_APP_SECRET (webhook signature
verification).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_VERIFY_TOKEN = "wp-bot-dev-verify-token"

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "inbound.db"


@dataclass(frozen=True)
class Settings:
    verify_token: str = DEFAULT_VERIFY_TOKEN
    track_b_url: str = "http://127.0.0.1:8200"
    db_path: Path = _DEFAULT_DB_PATH
    api_token: str = ""  # WhatsApp system-user access token (Media + Messages API)
    phone_number_id: str = ""  # business phone number id (outbound messages)
    app_secret: str = ""  # Meta app secret; verifies X-Hub-Signature-256 on deliveries
    api_version: str = "v21.0"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", DEFAULT_VERIFY_TOKEN),
            track_b_url=os.environ.get("TRACK_B_URL", "http://127.0.0.1:8200"),
            db_path=Path(
                os.environ.get(
                    "WP_BOT_TRACK_A_DB",
                    str(_DEFAULT_DB_PATH),
                )
            ),
            api_token=os.environ.get("WHATSAPP_API_TOKEN", ""),
            phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
            app_secret=os.environ.get("WHATSAPP_APP_SECRET", ""),
            api_version=os.environ.get("WHATSAPP_GRAPH_API_VERSION", "v21.0"),
        )
