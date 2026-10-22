"""Focus pages: the newest visible items plus one stub per collapsed run.

`GET /messages?view=focus` walks display anchors newest-first, returns the
newest N rows Focus shows on their own, and reduces the hidden rows between
them to run stubs. A run's rows load only when it is expanded, through the
inclusive range read in `read_authorized_run_range`.

The walk reads only the columns it needs, runs in one read snapshot, and stops
after `scan_cap` rows so a thread with very few visible rows cannot make one
page unbounded. The one hidden-row body it reads is a run's rows that mention
`browser_open`, to find the stub's browser chip.
"""

from __future__ import annotations

import json
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

# The anchors the walk may inspect, newest-first: the one definition of the row set,
# of visibility (`FOCUS_VISIBLE_SQL`) and of a row's Turn, shared by both walk
# statements; a row the transcript never draws (`NEVER_DRAWN_FILTER`) is not in it.
# `:after` is the cursor's seq (or NULL), `:floor` an inclusive lower seq
# bound (or NULL) and `:newest_n` the most rows read.
_VISIBLE_COLUMN = f"{FOCUS_VISIBLE_SQL} AS vis"
_RUN_COLUMNS = (
    f"{_VISIBLE_COLUMN}, {ACTIVITY_WEIGHT_SQL} AS weight, "
    f"{focus_kind_sql(ACTIVITY_WEIGHT_SQL)} AS row_kind, "
    "(role = 'assistant' AND instr(metadata, 'browser_open') > 0) AS opens, "
    "CASE WHEN json_valid(metadata) AND json_type(metadata, '$.turn_id') = 'text' "
    "AND json_extract(metadata, '$.turn_id') != '' "
    "THEN json_extract(metadata, '$.turn_id') END AS turn_id"
)


def _anchors_cte(columns: str) -> str:
    return (
        f"anchors AS (SELECT id, seq, {columns} "
        "FROM messages WHERE thread_id = :thread AND seq IS NOT NULL "
        f"{_DISPLAY_ANCHOR_FILTER}{NEVER_DRAWN_FILTER}"
        "AND (:after IS NULL OR seq < :after) AND (:floor IS NULL OR seq >= :floor) "
        "ORDER BY seq DESC LIMIT :newest_n)"
    )


# The newest `:budget` visible anchors among the first `:newest_n` anchors; only they
# cross into Python. When the budget fills, the last one's seq is where the walk stops.
_VISIBLE_SQL = (
    f"WITH {_anchors_cte(_VISIBLE_COLUMN)} "
    "SELECT id, seq FROM anchors WHERE vis = 1 ORDER BY seq DESC LIMIT :budget"
)

# Every hidden run among the first `:cap` anchors, aggregated in SQLite. A run closes
# at each visible anchor (`seg` counts the visible anchors above a row) and wherever
# two consecutive rows with a known Turn disagree on whether it is live; a row with no
# Turn stays with the run above it. `turn_ids` lists a run's Turns oldest-first, a
# repeat of the Turn just above collapsed. `kinds` is the distinct `row_kind` of a
# run's rows. `open_seqs` is the seq of each assistant row that mentions `browser_open`, found in the
# same pass, for the run's browser chip. One `meta` row reports how many anchors the
# CTE held (`count`, up to `:newest_n`) and the oldest one within the cap (`first_id`).
_RUNS_SQL = f"""
WITH {_anchors_cte(_RUN_COLUMNS)},
scanned AS (
    SELECT *, ROW_NUMBER() OVER (ORDER BY seq DESC) AS rn FROM anchors
),
bounded AS (
    SELECT id, seq, vis, weight, row_kind, turn_id, opens, rn,
        COALESCE(SUM(vis) OVER (ORDER BY seq DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS seg,
        CASE WHEN turn_id IS NOT NULL
            THEN turn_id IN (SELECT id FROM active_turns WHERE thread_id = :thread
                AND status IN ({_LIVE_STATUS_LIST})) END AS live
    FROM scanned WHERE rn <= :cap
),
hidden AS (
    SELECT *, MAX(CASE WHEN live IS NOT NULL THEN rn * 2 + live END) OVER (
        PARTITION BY seg ORDER BY seq DESC
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_known
    FROM bounded WHERE vis = 0
),
split AS (
    SELECT *, SUM(CASE WHEN live IS NOT NULL AND prev_known IS NOT NULL
        AND prev_known % 2 != live THEN 1 ELSE 0 END) OVER (
        PARTITION BY seg ORDER BY seq DESC ROWS UNBOUNDED PRECEDING) AS run_no
    FROM hidden
),
ends AS (
    SELECT *,
        FIRST_VALUE(id) OVER ends_window AS newest_id,
        LAST_VALUE(id) OVER ends_window AS oldest_id
    FROM split
    WINDOW ends_window AS (PARTITION BY seg, run_no ORDER BY seq DESC
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
),
turns AS (
    SELECT seg, run_no, seq, turn_id, LAG(turn_id) OVER (
        PARTITION BY seg, run_no ORDER BY seq DESC) AS newer_turn
    FROM split WHERE turn_id IS NOT NULL
),
run_turns AS (
    SELECT seg, run_no, json_group_array(turn_id ORDER BY seq ASC) AS turn_ids
    FROM turns WHERE newer_turn IS NULL OR newer_turn != turn_id
    GROUP BY seg, run_no
)
SELECT 'run' AS kind, MIN(ends.seq) AS first_seq, MAX(ends.seq) AS last_seq,
    COUNT(*) AS count, SUM(ends.weight) AS weight,
    MAX(ends.oldest_id) AS first_id, MAX(ends.newest_id) AS last_id,
    COALESCE(MAX(run_turns.turn_ids), '[]') AS turn_ids,
    json_group_array(DISTINCT ends.row_kind) AS kinds,
    json_group_array(ends.seq) FILTER (WHERE ends.opens) AS open_seqs
FROM ends LEFT JOIN run_turns USING (seg, run_no)
GROUP BY ends.seg, ends.run_no
UNION ALL
SELECT 'meta', NULL, NULL, (SELECT COUNT(*) FROM anchors), 0,
    (SELECT id FROM scanned WHERE rn = MIN((SELECT COUNT(*) FROM anchors), :cap)),
    NULL, NULL, '[]', NULL
ORDER BY first_seq
"""

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
    # Per run, parallel to `runs`: the seqs of its rows that may have opened the shared browser.
    open_seqs: list[list[int]] = field(default_factory=list)
    oldest_id: str | None = None
    has_more: bool = False
    scanned: int = 0


def _walk(
    db, thread_id: str, cursor_seq: int | None,
    budget: int, cap: int,
) -> _Walk:
    """Walk newest-first until `budget` visible anchors or `cap` rows are read.

    Two statements, both aggregating in SQLite: the first finds the visible anchors
    (and so where the walk stops), the second reduces the hidden rows in that range
    to run stubs. Only visible anchors and stubs cross into Python.
    """
    args = {"thread": thread_id, "after": cursor_seq, "floor": None, "cap": cap}
    visible = db.execute(_VISIBLE_SQL, {**args, "newest_n": cap, "budget": budget}).fetchall()
    walk = _Walk(visible_ids=[row["id"] for row in reversed(visible)])
    floor = visible[-1]["seq"] if len(visible) >= budget else None
    rows = db.execute(_RUNS_SQL, {**args, "floor": floor, "newest_n": cap + 1}).fetchall()
    meta = next(row for row in rows if row["kind"] == "meta")
    walk.oldest_id = meta["first_id"]
    walk.scanned = min(meta["count"], cap)
    if floor is None:
        walk.has_more = meta["count"] > cap
    else:
        walk.has_more = db.execute(_OLDER_EXISTS_SQL, (thread_id, floor)).fetchone() is not None
    runs = [row for row in rows if row["kind"] == "run"]
    for row in runs:
        walk.runs.append({
            "kind": "run",
            "first_id": row["first_id"],
            "last_id": row["last_id"],
            "first_seq": row["first_seq"],
            "last_seq": row["last_seq"],
            "count": row["count"],
            "turn_ids": json.loads(row["turn_ids"]),
            "weight": int(row["weight"] or 0),
            "kinds": sorted(json.loads(row["kinds"])),
            "continues_older": False,
        })
        walk.open_seqs.append(json.loads(row["open_seqs"] or "[]"))
    # The oldest run is cut off by the cap or budget only if it holds the last row read.
    if walk.runs and walk.runs[0]["first_id"] == walk.oldest_id:
        walk.runs[0]["continues_older"] = walk.has_more
    return walk


def _attach_browser_chips(db, thread_id: str, runs: list[dict], open_seqs: list[list[int]]) -> None:
    """Set `browser_chip` on each run stub whose rows put the shared browser in front of the person.

    The run query already named each run's rows that mention `browser_open`. Only those are read and
    projected as the page projects any row, and the last call that carries the display projection's
    `browser_chip` wins; the stub holds no row body.
    """
    owner = {seq: run for run, seqs in zip(runs, open_seqs) for seq in seqs}
    if not owner:
        return
    marks = ",".join("?" for _ in owner)
    rows = db.execute(
        f"SELECT *, rowid FROM messages WHERE thread_id = ? AND seq IN ({marks}) ORDER BY seq",
        [thread_id, *owner],
    ).fetchall()
    for row in project_anchors(db, thread_id, [dict(row) for row in rows]):
        metadata = json.loads(row["metadata"]) if isinstance(row.get("metadata"), str) else row.get("metadata")
        for call in (metadata or {}).get("tool_calls") or []:
            if isinstance(call, dict) and call.get("browser_chip"):
                owner[row["seq"]]["browser_chip"] = call["browser_chip"]


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
    _attach_browser_chips(db, thread_id, walk.runs, walk.open_seqs)
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
