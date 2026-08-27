"""Structured logging setup shared by both Track A and Track B.

When ``JSON_LOGS=true`` is set, logs are emitted as JSON lines (via
``python-json-logger``) for easy ingestion by log aggregators.
Otherwise the default human-readable format is used.

Usage::

    from shared_contract.logging import setup_logging
    setup_logging(json_format=os.environ.get("JSON_LOGS", "").lower() == "true")
"""

from __future__ import annotations

import logging
import os
import sys


def setup_logging(
    *,
    json_format: bool | None = None,
    level: int | None = None,
) -> None:
    """Configure root logging for the application.

    Parameters
    ----------
    json_format:
        If ``True``, emit JSON logs.  If ``None``, reads ``JSON_LOGS``
        env var (``"true"`` → JSON).
    level:
        Log level.  Defaults to ``INFO``.
    """
    if json_format is None:
        json_format = os.environ.get("JSON_LOGS", "").lower() == "true"
    if level is None:
        level = logging.INFO

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers to avoid duplicates.
    root.handlers.clear()

    if json_format:
        try:
            from pythonjsonlogger import json as jsonlogger

            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                jsonlogger.JsonFormatter(
                    fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
                    rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
                )
            )
        except ImportError:
            # Fallback: basic structured format without the json lib.
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
            )
            logging.getLogger("shared_contract.logging").warning(
                "python-json-logger not installed; falling back to text format. "
                "Install with: pip install python-json-logger"
            )
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        )

    root.addHandler(handler)
