"""Outbound message composition (A5).

Pure functions: a validated intent in, a plain-language message out.
Every confirmation message ends with an explicit YES/NO instruction so
the owner always knows how to respond. Exact strings are module-level
constants so tests can assert them verbatim.
"""

from __future__ import annotations

from typing import Any

CONFIRM_PUBLISH = "Reply YES to publish, NO to cancel."
CONFIRM_CHANGE = "Reply YES to confirm, NO to cancel."

CANCEL_REPLY_TEXT = "Okay, cancelled — nothing was changed."

GENERIC_ERROR_REPLY_TEXT = (
    "Something went wrong on our end — nothing was published. Want to try again?"
)

UNDO_GENERIC_ERROR_TEXT = (
    "Something went wrong while trying to undo that change — nothing was "
    "reverted. Please try again."
)


def compose_confirmation(intent: dict[str, Any]) -> str:
    """Human-readable confirmation for a validated, complete intent."""
    action = intent.get("action", "")
    content_type = intent.get("content_type", "")
    fields = intent.get("fields") or {}

    if content_type == "job":
        return _compose_job(action, fields)
    if content_type == "announcement":
        return _compose_announcement(action, fields)
    if content_type == "business_info":
        return _compose_business_info(fields)
    if content_type == "image":
        return _compose_image(action, fields)

    return f"I'd like to {action} your {content_type}. {CONFIRM_CHANGE}"


def _compose_job(action: str, fields: dict[str, Any]) -> str:
    title = str(fields.get("title") or "this job")
    if action == "delete":
        return f"Remove job '{title}'. Reply YES to remove, NO to cancel."

    if action == "update":
        # title identifies the job; summarize only what changed.
        changed = {k: v for k, v in fields.items() if k != "title"}
        summary = _field_values(changed)
        if summary:
            return f"Update job '{title}': {summary}. {CONFIRM_PUBLISH}"
        return f"Update job '{title}'. {CONFIRM_PUBLISH}"

    # create
    meta: list[str] = []
    if fields.get("location"):
        meta.append(str(fields["location"]))
    if fields.get("remote") is not None:
        meta.append("remote" if fields["remote"] else "on-site")

    parts = [f"Post job: '{title}'"]
    if meta:
        parts[0] += f" — {', '.join(meta)}"
    if fields.get("description"):
        parts.append(str(fields["description"]))
    return f"{'. '.join(parts)}. {CONFIRM_PUBLISH}"


def _compose_announcement(action: str, fields: dict[str, Any]) -> str:
    title = str(fields.get("title") or "this announcement")
    body = str(fields.get("body") or "").strip()

    if action == "delete":
        return f"Remove announcement '{title}'. Reply YES to remove, NO to cancel."
    if action == "update":
        if body:
            return f"Update announcement '{title}' to: '{body}'. {CONFIRM_PUBLISH}"
        return f"Update announcement '{title}'. {CONFIRM_PUBLISH}"

    # create
    if body:
        return f"Post announcement: '{title}'. {body}. {CONFIRM_PUBLISH}"
    return f"Post announcement: '{title}'. {CONFIRM_PUBLISH}"


def _compose_business_info(fields: dict[str, Any]) -> str:
    names = list(fields)
    if not names:
        return f"Update your business info. {CONFIRM_CHANGE}"
    if len(names) == 1:
        label = names[0]
    else:
        label = ", ".join(names[:-1]) + f" and {names[-1]}"
    return f"Update your {label} to: {_field_values(fields)}. {CONFIRM_CHANGE}"


def _compose_image(action: str, fields: dict[str, Any]) -> str:
    slot = str(fields.get("slot") or "image").replace("_", " ")
    if action == "create":
        return f"Set your {slot} to the photo you sent. {CONFIRM_CHANGE}"
    if action == "delete":
        return f"Remove your {slot} image. {CONFIRM_CHANGE}"
    return f"Replace your {slot} with the photo you sent. {CONFIRM_CHANGE}"


def _field_values(fields: dict[str, Any]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in fields.items())


def compose_completion(live_url: str | None) -> str:
    """Post-publish completion message (result status \"success\")."""
    if live_url:
        return (
            f"Done! Here's the live change: {live_url}. "
            "You can undo this within 24h by replying UNDO."
        )
    return "Done! The change is live. You can undo this within 24h by replying UNDO."


def compose_error(error_message: str | None) -> str:
    """Plain-language error (result status \"failed\" or transport failure).

    Never a silent failure, never a false \"done\": uses the publisher's
    error_message when present, otherwise the generic text.
    """
    if error_message:
        return f"Something went wrong: {error_message}. Nothing was published. Want to try again?"
    return GENERIC_ERROR_REPLY_TEXT


def compose_cancelled() -> str:
    """Reply when the owner declines a confirmed change (NO)."""
    return CANCEL_REPLY_TEXT


def compose_undo_done(live_url: str | None) -> str:
    """Reply when Track B reports the owner's last change was reverted."""
    if live_url:
        return f"Done — your last change has been reverted. Here's the live state: {live_url}."
    return "Done — your last change has been reverted."


def compose_undo_error(error_message: str | None) -> str:
    """Plain-language undo failure — never a silent no-op.

    Uses Track B's error_message (e.g. "nothing to undo", "outside the
    24h undo window") when present; otherwise the generic text.
    """
    if error_message:
        return f"Couldn't undo that change: {error_message}"
    return UNDO_GENERIC_ERROR_TEXT
