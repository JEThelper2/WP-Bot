"""Shared contract validation for WP-Bot.

Track A (WhatsApp conversation service) and Track B (WordPress
site/state service) both import these helpers and validate every
message at their boundary — never trust the other side.

See the README in this directory for the full contract rules.
"""

from .url import normalize_url
from .validator import (
    CONTRACT_VERSION,
    ContractValidationError,
    validate_intent,
    validate_result,
)

__all__ = [
    "CONTRACT_VERSION",
    "ContractValidationError",
    "normalize_url",
    "validate_intent",
    "validate_result",
]
