"""Shared URL normalization used by both Track A and Track B onboarding.

Extracted from near-identical ``_plausible_url()`` (Track A) and
``_normalize_url()`` (Track B) to a single source of truth in the
shared-contract package.
"""

from __future__ import annotations

from urllib.parse import urlparse


def normalize_url(raw: str) -> str | None:
    """Return a normalized http(s) URL, or None if the input is invalid.

    Accepts bare domains (``example.com``), adds ``https://`` when no
    scheme is present, strips trailing slashes, and rejects anything
    that doesn't look like a plausible website address.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.split("@")[-1].split(":")[0]
    if not host or ("." not in host and host not in ("localhost",)):
        return None
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
