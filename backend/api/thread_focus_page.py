"""Focus pages: the newest visible items plus one stub per collapsed run.

`GET /messages?view=focus` walks display anchors newest-first, returns the
newest N rows Focus shows on their own, and reduces the hidden rows between
them to run stubs. A run's rows load only when it is expanded, through the
inclusive range read in `read_authorized_run_range`.

The walk reads only the columns it needs (never a hidden row's body), runs in
one read snapshot, and stops after `scan_cap` rows so a thread with very few
visible rows cannot make one page unbounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from infra.focus_rows import FOCUS_VISIBLE_SQL, NEVER_DRAWN_FILTER, focus_kind_sql
from api.thread_paging import (
    _DISPLAY_ANCHOR_FILTER,
    CursorNotFound,
    finish_display,
    project_anchors,
)
from api.thread_turn_summaries import page_turn_summaries
from history.projection import in_read_snapshot
from infra.message_activity import ACTIVITY_WEIGHT_SQL, DISPLAY_ROW_SELECT
from infra.queries import get_thread_for_user

# Rows one Focus page may inspect. A page of a 0.7%-visible thread needs about 7100.
# Both walk statements run inside SQLite, so a row costs about 0.02 ms warm (the
# 8,153-row page took about 200 ms) and, unlike a row crossing
# into Python, does not wait for the interpreter lock under load.
FOCUS_SCAN_CAP = 12000
_LIVE_STATUSES = ("running", "waiting_retry")
_LIVE_STATUS_LIST = ",".join(f"'{status}'" for status in _LIVE_STATUSES)

# The one bounded anchor read for a Focus page. It classifies each row once in
# SQLite, then the small run-reduction loop below avoids re-scanning the same anchors
# through several window functions. Hidden message bodies never cross into Python.
_VISIBLE_COLUMN = f"{FOCUS_VISIBLE_SQL} AS vis"
_RUN_COLUMNS = (
    f"role, {_VISIBLE_COLUMN}, {ACTIVITY_WEIGHT_SQL} AS weight, "
    f"CASE WHEN role != 'assistant' THEN {focus_kind_sql('0')} ELSE 'other' END "
    "AS non_assistant_row_kind, "
    "CASE WHEN json_valid(metadata) AND json_type(metadata, '$.turn_id') = 'text' "
    "AND json_extract(metadata, '$.turn_id') != '' "
    "THEN json_extract(metadata, '$.turn_id') END AS turn_id"
)
_WALK_ROWS_SQL = (
    f"SELECT id, seq, {_RUN_COLUMNS} FROM messages WHERE thread_id = :thread "
    "AND seq IS NOT NULL "
    f"{_DISPLAY_ANCHOR_FILTER}{NEVER_DRAWN_FILTER}"
    "AND (:after IS NULL OR seq < :after) ORDER BY seq DESC LIMIT :newest_n"
)

_OLDER_EXISTS_SQL = (
    "SELECT 1 FROM messages WHERE thread_id = ? AND seq IS NOT NULL AND seq < ? "
    f"{_DISPLAY_ANCHOR_FILTER}{NEVER_DRAWN_FILTER}LIMIT 1"
)


class InvertedRange(ValueError):
    """`from_id` is newer than `to_id`; the route maps it to 400."""


@dataclass
class _Walk:
    visible_ids: list[str] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    oldest_id: str | None = None
    has_more: bool = False
    scanned: int = 0


def _reduce_runs(rows: list, active_turn_ids: set[str]) -> list[dict]:
    """Reduce newest-first classified anchors to oldest-first Focus run stubs."""
    groups: list[dict] = []
    current: dict | None = None
    previous_live: bool | None = None
    previous_turn: str | None = None

    for row in rows:
        if row["vis"]:
            current = None
            previous_live = None
            previous_turn = None
            continue

        turn_id = row["turn_id"]
        live = turn_id in active_turn_ids if turn_id is not None else None
        if live is not None and previous_live is not None and live != previous_live:
            current = None
            previous_turn = None
        if live is not None:
            previous_live = live
        if current is None:
            current = {
                "kind": "run", "first_id": row["id"], "last_id": row["id"],
                "first_seq": row["seq"], "last_seq": row["seq"], "count": 0,
                "turn_ids": [], "weight": 0, "kinds": set(), "continues_older": False,
            }
            groups.append(current)

        current["first_id"] = row["id"]
        current["first_seq"] = row["seq"]
        current["count"] += 1
        current["weight"] += int(row["weight"] or 0)
        current["kinds"].add(
            "steps" if row["role"] == "assistant" and row["weight"] > 0
            else row["non_assistant_row_kind"]
        )
        if turn_id is not None and turn_id != previous_turn:
            current["turn_ids"].append(turn_id)
            previous_turn = turn_id

    runs = list(reversed(groups))
    for run in runs:
        run["turn_ids"].reverse()
        run["kinds"] = sorted(run["kinds"])
    return runs


def _walk(
    db, thread_id: str, cursor_seq: int | None,
    budget: int, cap: int,
) -> _Walk:
    """Walk newest-first until `budget` visible anchors or `cap` rows are read."""
    cursor = db.execute(
        _WALK_ROWS_SQL,
        {"thread": thread_id, "after": cursor_seq, "newest_n": cap + 1},
    )
    anchors = []
    visible = []
    has_extra = False
    for row in cursor:
        if len(anchors) == cap:
            has_extra = True
            break
        anchors.append(row)
        if row["vis"]:
            visible.append(row)
            if len(visible) == budget:
                break
    cursor.close()

    walk = _Walk(visible_ids=[row["id"] for row in reversed(visible)], scanned=len(anchors))
    if len(visible) == budget:
        last_visible = visible[-1]
        walk.oldest_id = last_visible["id"]
        walk.has_more = db.execute(
            _OLDER_EXISTS_SQL, (thread_id, last_visible["seq"]),
        ).fetchone() is not None
    else:
        walk.oldest_id = anchors[-1]["id"] if anchors else None
        walk.has_more = has_extra

    active_turn_ids = {
        row["id"] for row in db.execute(
            "SELECT id FROM active_turns WHERE thread_id = ? AND status IN (?, ?)",
            (thread_id, *_LIVE_STATUSES),
        ).fetchall()
    }
    walk.runs = _reduce_runs(anchors, active_turn_ids)
    # The oldest run is cut off by the cap or budget only if it holds the last row read.
    if walk.runs and walk.runs[0]["first_id"] == walk.oldest_id:
        walk.runs[0]["continues_older"] = walk.has_more
    return walk


def _unsequenced(db, thread_id: str) -> list[dict]:
    """Every unsequenced display row (queued inputs), in delivery (FIFO) order.

    The head of the queue delivers next and carries the waiting or failed state with
    its Retry and Cancel, so none may be dropped; no older page returns them.
    """
    rows = db.execute(
        DISPLAY_ROW_SELECT + "WHERE thread_id = ? AND seq IS NULL "
        f"{_DISPLAY_ANCHOR_FILTER}{NEVER_DRAWN_FILTER}"
        "ORDER BY input_order IS NULL, input_order ASC, created_at ASC, rowid ASC",
        (thread_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _anchors_in_order(db, thread_id: str, ids: list[str]) -> list[dict]:
    """Full rows for the given ids, in the order given."""
    found: dict[str, dict] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        for row in db.execute(
            DISPLAY_ROW_SELECT + f"WHERE thread_id = ? AND id IN ({marks})",
            [thread_id, *chunk],
        ).fetchall():
            found[row["id"]] = dict(row)
    return [found[mid] for mid in ids if mid in found]


def _cursor_seq(db, thread_id: str, before: str | None) -> int | None:
    if not before:
        return None
    row = db.execute(
        "SELECT seq FROM messages WHERE id = ? AND thread_id = ?", (before, thread_id),
    ).fetchone()
    if not row:
        raise CursorNotFound(before)
    seq = row["seq"]
    return int(seq) if isinstance(seq, int) and seq > 0 else None


def _visibility_by_id(db, thread_id: str, ids: list[str]) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        for row in db.execute(
            f"SELECT id, {FOCUS_VISIBLE_SQL} AS focus_visible FROM messages "
            f"WHERE thread_id = ? AND id IN ({marks})",
            [thread_id, *chunk],
        ).fetchall():
            result[row["id"]] = bool(row["focus_visible"])
    return result


def page_focus(
    db, thread_id: str, limit: int, before: str | None,
    *, scan_cap: int = FOCUS_SCAN_CAP,
) -> dict:
    """One Focus page: visible rows, run stubs, and the cursor for older rows."""
    if limit < 1:
        raise ValueError("`view=focus` requires `limit` >= 1")
    cursor_seq = _cursor_seq(db, thread_id, before)
    # Queued inputs have no position yet: they sit at the newest end of the first
    # page, count against its limit, and are never a cursor or inside a stub.
    queued = [] if before else _unsequenced(db, thread_id)
    walk = _walk(
        db, thread_id, cursor_seq,
        max(limit - len(queued), 1), scan_cap,
    )
    selected = _anchors_in_order(db, thread_id, walk.visible_ids)
    sequenced = project_anchors(db, thread_id, selected)
    queued_display = project_anchors(db, thread_id, queued)
    display = [*sequenced, *queued_display]
    flags = _visibility_by_id(db, thread_id, [row["id"] for row in queued_display])
    finish_display(db, thread_id, display, include_tool_results=False)
    for row in display:
        row["focus_visible"] = flags.get(row["id"], True)
    summary_rows = [
        *display,
        *({"metadata": {"turn_id": turn_id}} for run in walk.runs for turn_id in run["turn_ids"]),
    ]
    return {
        "messages": display,
        "runs": walk.runs,
        "has_more": walk.has_more,
        "oldest_id": walk.oldest_id,
        "turn_summaries": page_turn_summaries(db, thread_id, summary_rows),
    }


def _display_anchor_seq(db, thread_id: str, message_id: str) -> int:
    row = db.execute(
        "SELECT seq FROM messages WHERE id = ? AND thread_id = ? AND seq IS NOT NULL "
        f"{_DISPLAY_ANCHOR_FILTER}",
        (message_id, thread_id),
    ).fetchone()
    if not row:
        raise CursorNotFound(message_id)
    return int(row["seq"])


def page_run_range(
    db, thread_id: str, limit: int, from_id: str, to_id: str, before: str | None,
) -> dict:
    """One page, newest-first, of the display anchors from `from_id` to `to_id`.

    Either end may name a row the transcript never draws; the page never holds one.
    """
    if limit < 1:
        raise ValueError("a range read requires `limit` >= 1")
    low = _display_anchor_seq(db, thread_id, from_id)
    high = _display_anchor_seq(db, thread_id, to_id)
    if low > high:
        raise InvertedRange(f"from_id {from_id} is newer than to_id {to_id}")
    upper = high + 1
    if before:
        cursor = _cursor_seq(db, thread_id, before)
        if cursor is not None:
            upper = min(upper, cursor)
    rows = db.execute(
        DISPLAY_ROW_SELECT + "WHERE thread_id = ? "
        f"{_DISPLAY_ANCHOR_FILTER}{NEVER_DRAWN_FILTER}AND seq >= ? AND seq < ? "
        "ORDER BY seq DESC LIMIT ?",
        (thread_id, low, upper, limit + 1),
    ).fetchall()
    newest_first = [dict(row) for row in rows]
    has_more = len(newest_first) > limit
    selected = list(reversed(newest_first[:limit]))
    display = project_anchors(db, thread_id, selected)
    finish_display(db, thread_id, display, include_tool_results=False)
    return {
        "messages": display,
        "has_more": has_more,
        "oldest_id": selected[0]["id"] if selected else None,
        "turn_summaries": page_turn_summaries(db, thread_id, display),
    }


def _authorized(db, thread_id: str, user_id: str | None) -> bool:
    return user_id is None or bool(get_thread_for_user(thread_id, user_id, db=db))


def _focus_read(thread_id, limit, before, *, db, user_id=None, scan_cap=FOCUS_SCAN_CAP):
    if not _authorized(db, thread_id, user_id):
        return None
    return page_focus(db, thread_id, limit, before, scan_cap=scan_cap)


def _range_read(thread_id, limit, from_id, to_id, before, *, db, user_id=None):
    if not _authorized(db, thread_id, user_id):
        return None
    return page_run_range(db, thread_id, limit, from_id, to_id, before)


def read_authorized_focus_page(
    db, thread_id: str, limit: int, before: str | None,
    *, user_id: str | None = None, scan_cap: int = FOCUS_SCAN_CAP,
) -> dict | None:
    """Authorization and the Focus walk share one read snapshot."""
    return in_read_snapshot(
        db, _focus_read, thread_id, limit, before, user_id=user_id, scan_cap=scan_cap,
    )


def read_authorized_run_range(
    db, thread_id: str, limit: int, from_id: str, to_id: str, before: str | None,
    *, user_id: str | None = None,
) -> dict | None:
    """Authorization and the inclusive range read share one read snapshot."""
    return in_read_snapshot(
        db, _range_read, thread_id, limit, from_id, to_id, before, user_id=user_id,
    )
