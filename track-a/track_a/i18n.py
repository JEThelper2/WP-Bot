"""Internationalization (i18n) foundation for user-facing strings.

All user-facing text in the bot (composer confirmations, routing
questions, onboarding messages) should go through ``translate()`` to
support future locale expansion.  Currently only ``en`` is bundled;
adding a new locale is a single JSON file in ``locales/``.

Usage::

    from track_a.i18n import translate
    msg = translate("confirm_job_create", title="Barista", description="$18/hr")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("track_a.i18n")

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"
_cache: dict[str, dict[str, str]] = {}


def _load(locale: str) -> dict[str, str]:
    """Load a locale file, caching it for subsequent calls."""
    if locale in _cache:
        return _cache[locale]
    path = _LOCALES_DIR / f"{locale}.json"
    try:
        with open(path, encoding="utf-8") as f:
            data: dict[str, str] = json.load(f)
    except FileNotFoundError:
        logger.warning("Locale file not found: %s; falling back to 'en'", path)
        if locale != "en":
            return _load("en")
        data = {}
    _cache[locale] = data
    return data


def translate(key: str, locale: str = "en", **kwargs: Any) -> str:
    """Return the translated string for ``key`` in ``locale``.

    If the key is missing or the locale file doesn't exist, falls back
    to ``en``.  If the key is missing from ``en`` too, returns the raw
    key name (so missing translations are visible in the UI rather than
    silently becoming empty strings).

    Supports simple ``{name}`` placeholders via ``str.format_map``.
    """
    strings = _load(locale)
    text = strings.get(key)
    if text is None and locale != "en":
        # Fall back to English if the key doesn't exist in this locale.
        text = _load("en").get(key)
    if text is None:
        # Last resort: return the key itself so missing translations are visible.
        return key
    if kwargs:
        try:
            return text.format_map(kwargs)
        except KeyError:
            return text
    return text
