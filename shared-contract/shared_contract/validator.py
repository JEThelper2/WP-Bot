"""Validation helpers for the WP-Bot shared contract.

Both tracks validate every message at their boundary against the JSON
Schemas in this directory (intent.schema.json / result.schema.json).
The schemas are the single source of truth; these helpers are a thin
Python wrapper that raises a clear error naming the exact field that
failed validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

CONTRACT_VERSION = "1.0.0"

_CONTRACT_DIR = Path(__file__).resolve().parent.parent

try:
    with open(_CONTRACT_DIR / "intent.schema.json", encoding="utf-8") as _f:
        _INTENT_SCHEMA = json.load(_f)
    with open(_CONTRACT_DIR / "result.schema.json", encoding="utf-8") as _f:
        _RESULT_SCHEMA = json.load(_f)
except FileNotFoundError as _exc:
    raise RuntimeError(
        "Could not load the shared-contract schemas. Consume this package from the "
        "WP-Bot monorepo checkout (e.g. `pip install -e ./shared-contract`) so that "
        "intent.schema.json and result.schema.json sit next to the package."
    ) from _exc

_INTENT_VALIDATOR = Draft202012Validator(_INTENT_SCHEMA, format_checker=FormatChecker())
_RESULT_VALIDATOR = Draft202012Validator(_RESULT_SCHEMA, format_checker=FormatChecker())


class ContractValidationError(ValueError):
    """Raised when an object does not conform to the shared contract."""


def _validate(obj: Any, validator: Draft202012Validator, name: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ContractValidationError(
            f"{name} must be a JSON object, got {type(obj).__name__}"
        )

    version = obj.get("contract_version")
    if version != CONTRACT_VERSION:
        raise ContractValidationError(
            f"{name} carries contract_version {version!r} but this service speaks "
            f"{CONTRACT_VERSION!r}. Both tracks must agree on the contract version; "
            "see shared-contract/README.md."
        )

    errors = sorted(
        validator.iter_errors(obj),
        # Report the most specific (deepest) failure first so the boundary
        # names the exact field that failed.
        key=lambda e: (len(e.absolute_path), str(e.message)),
        reverse=True,
    )
    if errors:
        error = errors[0]
        path = error.json_path if error.json_path != "$" else "<root>"
        raise ContractValidationError(
            f"{name} failed validation at {path}: {error.message}"
        )
    return obj


def validate_intent(obj: Any) -> dict[str, Any]:
    """Validate an intent object (Track A -> Track B).

    Returns the object unchanged on success; raises
    ContractValidationError naming the failing field otherwise.
    """
    return _validate(obj, _INTENT_VALIDATOR, "intent")


def validate_result(obj: Any) -> dict[str, Any]:
    """Validate a result object (Track B -> Track A).

    Returns the object unchanged on success; raises
    ContractValidationError naming the failing field otherwise.
    """
    return _validate(obj, _RESULT_VALIDATOR, "result")


__all__ = [
    "CONTRACT_VERSION",
    "ContractValidationError",
    "validate_intent",
    "validate_result",
]
