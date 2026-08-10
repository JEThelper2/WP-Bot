"""Undo engine (PRD §11): reverse-apply of *stored state*, never a
re-guess by the LLM.

`undo(owner_id)` finds the owner's most recent change in the change log,
takes its `before` state, and reverse-applies it through the B1
WordPress client:

- create-undo  → delete the created post (from `after.post_id`);
- update-undo  → write the `before` field values back exactly;
- delete-undo  → re-create the deleted content from `before`;
- undo-of-undo → the shape rules below handle it too, so the trail stays
  fully reversible.

The reverse is derived from the row's SHAPE (what the before/after look
like), which makes original rows and undo rows work uniformly:

1. `before is None`            → the change created something → delete it.
2. `after` is a deleted-state  → the change ended in deletion → re-create
   from `before`.
3. `before` is a deleted-state → the change resurrected something (an
   undo of a delete) → delete the thing in `after`.
4. otherwise                  → both states are live posts → write the
   `before` fields back onto `before.post_id`.

The undo itself is logged as a new change row (`action="undo"`,
`undo_of=<original>`), so the audit trail stays complete and the undo is
itself undoable. A window (24h by default) rejects undos of changes older
than the window with a clear reason.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from .changelog import ChangeLog, ChangeRow
from .wordpress import WordPressClient, WordPressError

logger = logging.getLogger("track_b.undo")

# PRD suggests a 24h undo window.
UNDO_WINDOW_SECONDS = 24 * 60 * 60


class UndoResult:
    """Outcome of an undo attempt.

    status:
    - `undone`        — reverse-applied and logged.
    - `nothing_to_undo` — no change exists for this owner.
    - `window_passed`   — the change is older than the undo window.
    - `failed`        — the reverse-apply (or its log) failed; nothing
      was recorded as undone.
    """

    def __init__(
        self,
        status: str,
        message: str,
        *,
        change_id: str | None = None,
        original_change_id: str | None = None,
        live_url: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.message = message
        self.change_id = change_id
        self.original_change_id = original_change_id
        self.live_url = live_url
        self.before = before
        self.after = after

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"UndoResult(status={self.status!r}, message={self.message!r})"


def _is_deleted_state(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict):
        return False
    return state.get("deleted") is True or state.get("status") == "trash"


async def undo(
    owner_id: str,
    client: WordPressClient,
    changelog: ChangeLog,
    *,
    now_fn: Any = time.time,
    window: int = UNDO_WINDOW_SECONDS,
) -> UndoResult:
    """Undo the owner's most recent change, or explain why not."""
    row = await changelog.most_recent(owner_id)
    if row is None:
        return UndoResult(
            "nothing_to_undo", "no changes found to undo for this owner"
        )

    if now_fn() - row.timestamp > window:
        hours = window // 3600
        return UndoResult(
            "window_passed",
            f"change {row.change_id} is outside the {hours}h undo window — "
            "undo is no longer available for it",
            original_change_id=row.change_id,
        )

    try:
        record = await _reverse_apply(row, client)
    except WordPressError as exc:
        logger.warning("undo failed for owner %s (change %s): %s", owner_id, row.change_id, exc)
        return UndoResult(
            "failed",
            f"undo failed: {exc}",
            original_change_id=row.change_id,
        )

    # Log the undo itself — undo is undoable, and the trail stays complete.
    undo_row = ChangeRow(
        change_id=f"ch-{uuid.uuid4().hex[:12]}",
        owner_id=owner_id,
        content_type=row.content_type,
        action="undo",
        before=row.after,  # state before this undo
        after=record.after,  # state after this undo
        live_url=record.live_url,
        undo_of=row.change_id,
    )
    try:
        stamped = await changelog.record_change(undo_row)
    except Exception as exc:
        logger.error(
            "undo applied but could not be logged for owner %s: %s", owner_id, exc
        )
        return UndoResult(
            "failed",
            "the undo was applied but could not be logged — treat as not "
            "undone, and do not trust the live state",
            original_change_id=row.change_id,
            before=record.before,
            after=record.after,
        )

    logger.info("owner %s undone change %s -> %s", owner_id, row.change_id, stamped.change_id)
    return UndoResult(
        "undone",
        f"reverted change {row.change_id}",
        change_id=stamped.change_id,
        original_change_id=row.change_id,
        live_url=record.live_url,
        before=record.before,
        after=record.after,
    )


async def _reverse_apply(row: ChangeRow, client: WordPressClient) -> Any:
    """Reverse-apply a logged change via the B1 client (shape-based)."""
    before = row.before
    after = row.after

    if before is None:
        # The change created something: delete it.
        post_id = _post_id(after, row, "create-undo")
        return await client.delete_post(post_id, content_type=row.content_type)

    if _is_deleted_state(after):
        # The change ended in deletion: re-create from the stored state.
        return await client.create_post(row.content_type, _restore_fields(before))

    if _is_deleted_state(before):
        # The change resurrected content (undo of a delete): delete it again.
        post_id = _post_id(after, row, "delete-undo")
        return await client.delete_post(post_id, content_type=row.content_type)

    # Both are live post states: write the `before` fields back exactly.
    post_id = _post_id(before, row, "update-undo")
    return await client.update_post(
        post_id,
        _restore_fields(before),
        content_type=row.content_type,
    )


def _post_id(state: dict[str, Any] | None, row: ChangeRow, what: str) -> int:
    post_id = (state or {}).get("post_id")
    if post_id is None:
        raise WordPressError(
            f"cannot undo change {row.change_id}: no post id in the logged "
            f"state ({what})"
        )
    return int(post_id)


def _restore_fields(before: dict[str, Any]) -> dict[str, Any]:
    """Map a logged post-state back to client fields for the reverse write."""
    fields: dict[str, Any] = {
        "title": before.get("title"),
        "content": before.get("content"),
    }
    if before.get("status"):
        fields["status"] = before["status"]
    return fields
