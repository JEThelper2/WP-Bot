"""Outbound message composition (A5).

Pure functions: a validated intent in, a plain-language message out.
Every confirmation message ends with an explicit YES/NO instruction so
the owner always knows how to respond. All user-facing strings go
through ``translate()`` for i18n support.
"""

from __future__ import annotations

from typing import Any

from .i18n import translate


def _confirm_publish() -> str:
    return translate("confirm_publish")


def _confirm_change() -> str:
    return translate("confirm_change")


def _confirm_delete() -> str:
    return translate("confirm_delete")


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

    return translate("compose_generic", action=action, content_type=content_type, confirm=_confirm_change())


def _compose_job(action: str, fields: dict[str, Any]) -> str:
    title = str(fields.get("title") or "this job")
    confirm = _confirm_publish()
    if action == "delete":
        return translate("compose_job_delete", title=title, confirm=_confirm_delete())

    if action == "update":
        changed = {k: v for k, v in fields.items() if k != "title"}
        summary = _field_values(changed)
        if summary:
            return translate("compose_job_update", title=title, changes=summary, confirm=confirm)
        return translate("compose_job_update_no_changes", title=title, confirm=confirm)

    # create
    desc = str(fields.get("description") or "").strip()
    meta: list[str] = []
    if fields.get("location"):
        meta.append(str(fields["location"]))
    if fields.get("remote") is not None:
        meta.append("remote" if fields["remote"] else "on-site")

    if meta and desc:
        return translate("compose_job_create_meta", title=title, meta=", ".join(meta), description=desc, confirm=confirm)
    if meta:
        return translate("compose_job_create_meta_no_desc", title=title, meta=", ".join(meta), confirm=confirm)
    if desc:
        return translate("compose_job_create", title=title, description=desc, confirm=confirm)
    return translate("compose_job_create_no_desc", title=title, confirm=confirm)


def _compose_announcement(action: str, fields: dict[str, Any]) -> str:
    title = str(fields.get("title") or "this announcement")
    body = str(fields.get("body") or "").strip()
    confirm = _confirm_publish()

    if action == "delete":
        return translate("compose_announcement_delete", title=title, confirm=_confirm_delete())
    if action == "update":
        if body:
            return translate("compose_announcement_update", title=title, body=body, confirm=confirm)
        return translate("compose_announcement_update_no_body", title=title, confirm=confirm)

    # create
    if body:
        return translate("compose_announcement_create", title=title, body=body, confirm=confirm)
    return translate("compose_announcement_create_no_body", title=title, confirm=confirm)


def _compose_business_info(fields: dict[str, Any]) -> str:
    names = list(fields)
    if not names:
        return translate("compose_business_info_empty", confirm=_confirm_change())
    if len(names) == 1:
        label = names[0]
    else:
        label = ", ".join(names[:-1]) + f" and {names[-1]}"
    return translate("compose_business_info_update", label=label, values=_field_values(fields), confirm=_confirm_change())


def _compose_image(action: str, fields: dict[str, Any]) -> str:
    slot = str(fields.get("slot") or "image").replace("_", " ")
    confirm = _confirm_change()
    if action == "create":
        return translate("compose_image_create", slot=slot, confirm=confirm)
    if action == "delete":
        return translate("compose_image_delete", slot=slot, confirm=confirm)
    return translate("compose_image_replace", slot=slot, confirm=confirm)


def _field_values(fields: dict[str, Any]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in fields.items())


def compose_completion(live_url: str | None) -> str:
    """Post-publish completion message (result status \"success\")."""
    if live_url:
        return translate("completion_with_url", url=live_url)
    return translate("completion")


def compose_error(error_message: str | None) -> str:
    """Plain-language error (result status \"failed\" or transport failure).

    Never a silent failure, never a false \"done\": uses the publisher's
    error_message when present, otherwise the generic text.
    """
    if error_message:
        return translate("generic_error_with_reason", error=error_message)
    return translate("generic_error")


def compose_cancelled() -> str:
    """Reply when the owner declines a confirmed change (NO)."""
    return translate("cancel_reply")


def compose_undo_done(live_url: str | None) -> str:
    """Reply when Track B reports the owner's last change was reverted."""
    if live_url:
        return translate("undo_done_with_url", url=live_url)
    return translate("undo_done")


def compose_undo_error(error_message: str | None) -> str:
    """Plain-language undo failure — never a silent no-op.

    Uses Track B's error_message (e.g. "nothing to undo", "outside the
    24h undo window") when present; otherwise the generic text.
    """
    if error_message:
        return translate("undo_error_with_reason", error=error_message)
    return translate("undo_generic_error")
