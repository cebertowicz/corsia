"""Cursor-paging helpers for the session-messages API."""

from __future__ import annotations

import json

from history.projection import in_read_snapshot
from infra.queries import get_thread_for_user
from core.message_display import llm_to_display
from api.thread_turn_summaries import page_turn_summaries
from infra.message_activity import DISPLAY_ROW_SELECT
from api.thread_lineage import _fetch_lineage_rows, _parse_metadata  # noqa: F401


class CursorNotFound(Exception):
    """Raised when a `before` cursor id does not resolve in the session.

    Caller maps this to an empty page response (spec §3.6), NOT a 404.
    """


def _parse_message_metadata(display_msgs: list[dict]) -> None:
    """Parse metadata and remove CORSIA-owned persistence evidence for REST."""
    for row in display_msgs:
        if row.get("metadata") and isinstance(row["metadata"], str):
            try:
                row["metadata"] = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(row.get("metadata"), dict):
            row["metadata"] = {
                key: value
                for key, value in row["metadata"].items()
                if not key.startswith("_corsia_")
            }


def _defer_tool_results(display_msgs: list[dict]) -> None:
    """Defer stored tool output while preserving truly absent-result truth."""
    for row in display_msgs:
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            continue
        for call in metadata.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            if isinstance(call.get("result"), str):
                call.pop("result")
                call["result_state"] = "deferred"
            else:
                call.pop("result", None)
                call["result_state"] = "absent"


def load_tool_result(
    db, thread_id: str, assistant_id: str, tool_call_id: str,
) -> str | None:
    """Return one tool result attached to an assistant display step."""
    row = db.execute(
        DISPLAY_ROW_SELECT + "WHERE thread_id = ? AND id = ? AND role = 'assistant'",
        (thread_id, assistant_id),
    ).fetchone()
    if not row:
        return None
    display = llm_to_display(
        _fetch_lineage_rows(db, thread_id, [dict(row)]),
    )
    assistant = next((item for item in display if item.get("id") == assistant_id), None)
    metadata = _parse_metadata(assistant.get("metadata") if assistant else None)
    for call in metadata.get("tool_calls") or []:
        if isinstance(call, dict) and call.get("id") == tool_call_id:
            result = call.get("result")
            return result if isinstance(result, str) else None
    return None


def _project_delivery_intents(
    db,
    thread_id: str,
    display_msgs: list[dict],
) -> None:
    """Expose Queue only when the canonical input row says Queue."""
    message_ids = [
        str(row["id"])
        for row in display_msgs
        if isinstance(row.get("id"), str) and row["id"]
    ]
    if not message_ids:
        return
    placeholders = ",".join("?" for _ in message_ids)
    rows = db.execute(
        "SELECT id AS message_id,delivery_intent FROM messages "
        "WHERE thread_id=? AND id IN (" + placeholders + ")",
        [thread_id, *message_ids],
    ).fetchall()
    intents = {str(row["message_id"]): str(row["delivery_intent"]) for row in rows}
    for row in display_msgs:
        metadata = row.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("delivery_intent", None)
        if intents.get(row.get("id")) == "queue_after_turn":
            if not isinstance(metadata, dict):
                metadata = row["metadata"] = {}
            metadata["delivery_intent"] = "queue_after_turn"


_DISPLAY_ANCHOR_FILTER = (
    "AND role != 'tool' "
    "AND (role != 'system' OR "
    "COALESCE(CASE WHEN json_valid(metadata) "
    "THEN json_extract(metadata, '$.type') ELSE '' END, '') != 'result') "
)


def _has_unsequenced_anchors(db, thread_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id = ? AND seq IS NULL "
        "AND role != 'tool' LIMIT 1",
        (thread_id,),
    ).fetchone() is not None


def _fetch_display_anchors(
    db,
    thread_id: str,
    limit: int,
    cursor_seq: int | None,
    cursor_ts: str | None,
    cursor_rowid: int | None,
) -> list[dict]:
    """Fetch newest-first display anchors, excluding result-only children."""
    params: list[object] = [thread_id]
    if (cursor_ts is None or cursor_seq is not None) and not _has_unsequenced_anchors(
        db, thread_id,
    ):
        # Positions are unique per thread, so position order alone is display
        # order and the page reads straight down idx_messages_session_seq
        # instead of sorting every row in the thread.
        position_clause = ""
        if cursor_seq is not None:
            position_clause = "AND seq < ? "
            params.append(cursor_seq)
        params.append(limit + 1)
        return [dict(row) for row in db.execute(
            DISPLAY_ROW_SELECT + "WHERE thread_id = ? "
            f"{_DISPLAY_ANCHOR_FILTER}{position_clause}"
            "ORDER BY seq DESC LIMIT ?",
            params,
        ).fetchall()]

    # Legacy rows without a position page as newest, by wall clock. Each block
    # is read newest-first under its own LIMIT and only the union is sorted:
    # one ORDER BY over both would sort every row in the thread.
    page = limit + 1
    blocks: list[str] = []
    params = []
    positioned_clause = ""
    if cursor_ts is not None and cursor_seq is not None and cursor_seq > 0:
        # Past the unpositioned rows: only positions below the cursor remain.
        positioned_clause = "AND seq < ? "
    else:
        unpositioned_clause = ""
        params.append(thread_id)
        if cursor_ts is not None:
            unpositioned_clause = "AND (created_at < ? OR (created_at = ? AND rowid < ?)) "
            params.extend((cursor_ts, cursor_ts, cursor_rowid))
        params.append(page)
        blocks.append(
            f"SELECT * FROM ({DISPLAY_ROW_SELECT}WHERE thread_id = ? AND seq IS NULL "
            f"{_DISPLAY_ANCHOR_FILTER}{unpositioned_clause}"
            "ORDER BY created_at DESC, rowid DESC LIMIT ?)"
        )
    params.append(thread_id)
    if positioned_clause:
        params.append(cursor_seq)
    params.append(page)
    blocks.append(
        f"SELECT * FROM ({DISPLAY_ROW_SELECT}WHERE thread_id = ? AND seq IS NOT NULL "
        f"{_DISPLAY_ANCHOR_FILTER}{positioned_clause}"
        "ORDER BY seq DESC LIMIT ?)"
    )
    params.append(page)
    rows = db.execute(
        f"SELECT * FROM ({' UNION ALL '.join(blocks)}) "
        "ORDER BY CASE WHEN seq IS NULL THEN 1 ELSE 0 END DESC,"
        "seq DESC,created_at DESC,rowid DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _read_authorized_page(
    thread_id: str,
    limit: int | None,
    before: str | None,
    *,
    db,
    user_id: str | None = None,
) -> tuple[list[dict], bool, str | None] | None:
    """Read an authorized page while the caller owns the snapshot boundary."""
    if user_id is not None and not get_thread_for_user(thread_id, user_id, db=db):
        return None
    return page_messages(db, thread_id, limit, before, include_tool_results=False)


_page_turn_summaries = page_turn_summaries


def _read_authorized_page_with_summaries(
    thread_id: str,
    limit: int | None,
    before: str | None,
    *,
    db,
    user_id: str | None = None,
) -> tuple[list[dict], bool, str | None, dict[str, dict]] | None:
    page = _read_authorized_page(thread_id, limit, before, db=db, user_id=user_id)
    if page is None:
        return None
    display, has_more, oldest_id = page
    return display, has_more, oldest_id, page_turn_summaries(db, thread_id, display)


def read_authorized_page(
    db,
    thread_id: str,
    limit: int | None,
    before: str | None,
    *,
    user_id: str | None = None,
) -> tuple[list[dict], bool, str | None] | None:
    """Read authorization and page facts from the canonical history snapshot."""
    return in_read_snapshot(
        db, _read_authorized_page, thread_id, limit, before, user_id=user_id,
    )


def read_authorized_page_with_summaries(
    db,
    thread_id: str,
    limit: int | None,
    before: str | None,
    *,
    user_id: str | None = None,
) -> tuple[list[dict], bool, str | None, dict[str, dict]] | None:
    """Authorized message page plus additive whole-Turn summary facts."""
    return in_read_snapshot(
        db, _read_authorized_page_with_summaries, thread_id, limit, before, user_id=user_id,
    )


def project_anchors(db, thread_id: str, selected: list[dict]) -> list[dict]:
    """Project selected anchors, in order, with their lineage children attached."""
    selected_ids = {row["id"] for row in selected}
    projected = {
        row["id"]: row
        for row in llm_to_display(_fetch_lineage_rows(db, thread_id, selected))
        if row.get("id") in selected_ids
    }
    return [projected[row["id"]] for row in selected if row["id"] in projected]


def finish_display(
    db, thread_id: str, display: list[dict], *, include_tool_results: bool,
) -> None:
    """Shape projected rows for REST in place."""
    _parse_message_metadata(display)
    if not include_tool_results:
        _defer_tool_results(display)
    _project_delivery_intents(db, thread_id, display)
    for row in display:
        row.pop("rowid", None)


def page_messages(
    db,
    thread_id: str,
    limit: int | None,
    before: str | None,
    *,
    include_tool_results: bool = True,
) -> tuple[list[dict], bool, str | None]:
    """Fetch one page whose cursors and limits count display anchors only.

    Tool-result text may be deferred because the UI exposes it only on demand.
    """
    if before and (limit is None or limit < 1):
        raise ValueError(
            "`before` cursor requires `limit` ≥ 1 (spec §1.8); "
            "unbounded older-scan is forbidden"
        )

    cursor_seq: int | None = None
    cursor_ts: str | None = None
    cursor_rowid: int | None = None
    if before:
        cursor_row = db.execute(
            "SELECT seq,created_at,rowid FROM messages WHERE id = ? AND thread_id = ?",
            (before, thread_id),
        ).fetchone()
        if not cursor_row:
            raise CursorNotFound(before)
        cursor_seq = (
            int(cursor_row["seq"])
            if isinstance(cursor_row["seq"], int) and cursor_row["seq"] > 0
            else None
        )
        cursor_ts = cursor_row["created_at"]
        cursor_rowid = cursor_row["rowid"]

    if limit is None or limit < 1:
        raw = [dict(row) for row in db.execute(
            DISPLAY_ROW_SELECT + "WHERE thread_id = ? "
            "ORDER BY CASE WHEN seq IS NULL THEN 1 ELSE 0 END ASC,"
            "seq ASC,created_at ASC,rowid ASC",
            (thread_id,),
        ).fetchall()]
        display = llm_to_display(raw)
        has_more = False
        oldest_id = display[0]["id"] if display else None
    else:
        newest_first = _fetch_display_anchors(
            db, thread_id, limit, cursor_seq, cursor_ts, cursor_rowid,
        )
        has_more = len(newest_first) > limit
        selected = list(reversed(newest_first[:limit]))
        if not selected:
            return [], False, None
        display = project_anchors(db, thread_id, selected)
        oldest_id = selected[0]["id"]

    finish_display(db, thread_id, display, include_tool_results=include_tool_results)
    return display, has_more, oldest_id
