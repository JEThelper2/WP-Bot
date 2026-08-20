"""Track A configuration.

Values come from the environment so the same code runs in dev and
production. Defaults are dev-friendly; production deployments should
set WHATSAPP_VERIFY_TOKEN and WHATSAPP_APP_SECRET (webhook signature
verification).

AI provider config (Dependency Inversion):
    AI_PROVIDER              — intent parser provider (e.g. "groq").  Default: "groq".
    AI_FALLBACK_PROVIDER     — fallback on rate limit (e.g. "gemini").  Default: "".
    TRANSCRIPTION_PROVIDER   — voice transcription provider (e.g. "groq").  Default: "groq".
    <PROVIDER>_API_KEY       — provider-specific API key (e.g. GROQ_API_KEY).
    <PROVIDER>_MODEL         — optional model override (e.g. GROQ_MODEL).
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
    # AI provider config — injected at startup, not used by Settings directly.
    # The provider is selected and wired in create_app(); Settings only carries
    # the raw env values so tests can override them.
    ai_provider: str = "groq"
    ai_fallback_provider: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    transcription_provider: str = "groq"
    admin_token: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        ai_provider = os.environ.get("AI_PROVIDER", "groq")
        ai_fallback = os.environ.get("AI_FALLBACK_PROVIDER", "")
        transcription_provider = os.environ.get("TRANSCRIPTION_PROVIDER", "groq")
        # Provider-specific API key: GROQ_API_KEY, GEMINI_API_KEY, etc.
        ai_api_key_env = f"{ai_provider.upper()}_API_KEY"
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
            ai_provider=ai_provider,
            ai_fallback_provider=ai_fallback,
            ai_api_key=os.environ.get(ai_api_key_env, ""),
            ai_model=os.environ.get(f"{ai_provider.upper()}_MODEL", ""),
            transcription_provider=transcription_provider,
            admin_token=os.environ.get("ADMIN_TOKEN", ""),
        )
