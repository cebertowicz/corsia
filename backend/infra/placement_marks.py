"""The checkpoint placement mark: the one record of where a lesson went.

A checkpoint fork reviews a few unmarked lessons and marks each one; for a
``selected`` one it also answers where it goes, and the mark's detail keeps
that answer. The background worker (``core.placement_worker``) then places the
selected ones mechanically. The mark is the handoff and the receipt at once, so
there is no second store to drift from it: the worker's queue is simply the
entries whose mark says ``selected`` at their current revision.

A mark applies to one entry revision. Rewriting the entry bumps its revision,
which makes it unmarked again without anyone clearing anything. Writing a mark
never bumps a revision, so it cannot make a running fold's staged edits
conflict.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from loguru import logger

#: The continuity fields a lesson lives in (durable-write requirements §5a).
LEARNING_FIELDS = (
    "approvals_and_preferences",
    "active_constraints",
    "decisions",
    "rejected_alternatives",
    "known_failures",
)

#: At most this many entries in one checkpoint's review block.
REVIEW_BATCH = 5

#: At most this many entries waiting for the worker, workspace-wide. At the cap
#: no review block is offered, so entries simply stay unmarked until later.
OUTSTANDING_CAP = 32

MARK_STATES = (
    "reaffirmed", "selected", "written", "already", "proposed", "stays", "refused",
)

#: The verdicts ``continuity_edit`` takes for each placement operation.
REVIEW_VERDICTS = ("reaffirm", "select", "already")
PLACE_VERDICTS = ("written", "already", "proposed", "trial")

#: The worker's queue: active entries selected at their current revision, in
#: their thread's current history epoch. An entry left in an earlier epoch is
#: out of reach of any checkpoint, so it is neither queued nor counted.
_QUEUED = (
    "FROM continuity_entries e JOIN threads t "
    "ON t.id=e.thread_id AND t.history_epoch=e.history_epoch "
    "WHERE e.state='active' AND e.placement_state='selected' "
    "AND e.placement_revision=e.revision"
)


def log_outcome(
    *,
    thread_id: str,
    entry_id: int,
    revision: int,
    outcome: str,
    reason: str,
    destination: str = "",
    ref: str = "",
) -> None:
    """One server-log line per entry outcome; never the entry's text."""
    logger.info(
        "placement.outcome thread={} entry={}@{} outcome={} reason={} "
        "destination={} ref={}",
        thread_id, entry_id, revision, outcome, reason, destination or "-",
        ref or "-",
    )


def set_mark(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    history_epoch: int,
    entry_id: int,
    state: str,
    revision: int,
    detail: str | None = None,
) -> None:
    if state not in MARK_STATES:
        raise ValueError(f"unknown placement mark: {state!r}")
    conn.execute(
        "UPDATE continuity_entries SET placement_state=?,placement_revision=?,"
        "placement_detail=? WHERE thread_id=? AND history_epoch=? AND entry_id=?",
        (state, revision, detail, thread_id, history_epoch, entry_id),
    )


def clear_mark(
    conn: sqlite3.Connection, *, thread_id: str, history_epoch: int, entry_id: int
) -> None:
    """Return an entry to unmarked, so the next checkpoint considers it again."""
    conn.execute(
        "UPDATE continuity_entries SET placement_state=NULL,"
        "placement_revision=NULL,placement_detail=NULL "
        "WHERE thread_id=? AND history_epoch=? AND entry_id=?",
        (thread_id, history_epoch, entry_id),
    )


def _marked_at(conn: sqlite3.Connection, thread_id: str, epoch: int) -> dict[int, int]:
    rows = conn.execute(
        "SELECT entry_id,placement_revision FROM continuity_entries "
        "WHERE thread_id=? AND history_epoch=? AND placement_state IS NOT NULL",
        (thread_id, epoch),
    ).fetchall()
    return {int(row[0]): int(row[1]) for row in rows if row[1] is not None}


def review_candidates(
    conn: sqlite3.Connection,
    thread_id: str,
    history_epoch: int,
    frozen: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The unmarked lessons in a checkpoint's frozen view, oldest first.

    Read from the frozen snapshot so every candidate carries the exact revision
    the fork's ``review`` call is validated against.
    """
    marked = _marked_at(conn, thread_id, history_epoch)
    eligible = [
        entry for entry in frozen
        if entry["field"] in LEARNING_FIELDS
        and marked.get(int(entry["entry_id"])) != int(entry["revision"])
    ]
    eligible.sort(key=lambda entry: int(entry["entry_id"]))
    return eligible[:REVIEW_BATCH]


def outstanding_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(f"SELECT COUNT(*) {_QUEUED}").fetchone()[0])


def selected_entries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The worker's queue, oldest first, each with its stored placement answer.

    A selected mark's detail is always ``{checkpoint, answer}``: ``checkpoint``
    is the selecting checkpoint's ref, or null when the thread proposed the
    lesson in-turn. Neither route selects without an answer, and schema 46
    unmarked every selection made before the fork had to give one.
    """
    rows = conn.execute(
        "SELECT e.thread_id,e.history_epoch,e.entry_id,e.field,e.body,e.revision,"
        f"e.placement_detail {_QUEUED} ORDER BY e.created_at,e.entry_id LIMIT ?",
        (OUTSTANDING_CAP,),
    ).fetchall()
    queue = []
    for row in rows:
        detail = json.loads(str(row["placement_detail"]))
        queue.append({
            "thread_id": str(row["thread_id"]),
            "history_epoch": int(row["history_epoch"]),
            "entry_id": int(row["entry_id"]),
            "field": str(row["field"]),
            "body": json.loads(str(row["body"])),
            "revision": int(row["revision"]),
            "checkpoint": detail["checkpoint"],
            "answer": detail["answer"],
        })
    return queue


def current_entry(
    conn: sqlite3.Connection, thread_id: str, history_epoch: int, entry_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT state,revision,placement_state,placement_revision,body "
        "FROM continuity_entries WHERE thread_id=? AND history_epoch=? "
        "AND entry_id=?",
        (thread_id, history_epoch, entry_id),
    ).fetchone()


def stage_review(
    entries: dict[int, dict[str, Any]],
    prior: list[sqlite3.Row],
    params: dict[str, Any],
    *,
    insert: Callable[..., None],
    answer: Callable[[dict[str, Any], object, object], dict[str, Any]],
    verify: Callable[[str, str], str],
) -> dict[str, Any]:
    """Stage one checkpoint verdict for an unchanged lesson in the frozen view.

    ``answer(entry, placement, room)`` checks a ``select`` verdict's
    placement answer and returns what the mark keeps, and
    ``verify(destination, quote)`` checks an ``already`` verdict's file says
    it; each raises ``ContinuityError`` with what the fork must correct.
    """
    from infra.continuity_store import ContinuityError

    entry_id = int(params["entry_id"])
    target = entries.get(entry_id)
    if target is None or target.get("revision") is None:
        raise ContinuityError(
            f"continuity entry {entry_id} is not in this checkpoint's review"
        )
    field = str(target["field"])
    if field not in LEARNING_FIELDS:
        raise ContinuityError(
            f"review applies to {', '.join(LEARNING_FIELDS)}, not {field}"
        )
    if any(
        int(row["entry_id"]) == entry_id and row["operation"] != "add"
        for row in prior
    ):
        raise ContinuityError(
            f"continuity entry {entry_id} already has a staged edit or verdict "
            "in this checkpoint"
        )
    verdict = params.get("verdict")
    if verdict not in REVIEW_VERDICTS:
        raise ContinuityError(
            f"review verdict must be one of {', '.join(REVIEW_VERDICTS)}"
        )
    destination = str(params.get("destination") or "").strip()
    quote = str(params.get("quote") or "")
    if verdict == "already" and not (destination and quote.strip()):
        raise ContinuityError("verdict already requires destination and quote")
    if verdict != "already" and (destination or quote):
        raise ContinuityError("destination and quote go only with verdict already")
    placement, room = params.get("placement"), params.get("room")
    if verdict != "select" and (placement is not None or room is not None):
        raise ContinuityError("placement and room go only with verdict select")
    if verdict == "select" and placement is None:
        raise ContinuityError("verdict select requires placement")
    stored = answer(target, placement, room) if verdict == "select" else None
    if verdict == "already":
        destination = verify(destination, quote)
    body = json.dumps(
        {"verdict": verdict, "destination": destination or None,
         "quote": quote or None, "answer": stored},
        sort_keys=True, separators=(",", ":"),
    )
    insert(
        operation="review", entry_id=entry_id, field=field, body=body,
        base_revision=int(target["revision"]),
    )
    staged = {"operation": "review", "entry_id": entry_id, "field": field,
              "verdict": verdict}
    if stored and stored.get("hold"):
        staged["hold"] = stored["hold"]
    return staged


def apply_review(
    conn: sqlite3.Connection,
    checkpoint_id: str,
    row: sqlite3.Row,
    common: dict[str, Any],
) -> None:
    """Publish one staged verdict as the entry's mark, inside the install.

    An ``already`` verdict is checked again against the file as it is now:
    if it no longer says the lesson, the verdict conflicts and the entry stays.
    """
    from core.placement_receipt import NotThere, verify
    from infra.continuity_store import (
        ContinuityConflict,
        ContinuityError,
        retire_entry,
    )

    entry_id = int(row["entry_id"])
    revision = int(row["base_entry_revision"])
    verdict = json.loads(str(row["body"]))
    source = f"checkpoint:{checkpoint_id}"
    if verdict["verdict"] == "reaffirm":
        set_mark(
            conn, **common, entry_id=entry_id, state="reaffirmed",
            revision=revision, detail=source,
        )
        return
    if verdict["verdict"] == "select":
        detail = json.dumps(
            {"checkpoint": source, "answer": verdict["answer"]},
            ensure_ascii=False, sort_keys=True,
        )
        set_mark(
            conn, **common, entry_id=entry_id, state="selected",
            revision=revision, detail=detail,
        )
        return
    try:
        destination = verify(
            conn, common["thread_id"], str(verdict["destination"]),
            str(verdict.get("quote") or ""),
        )
    except NotThere as missing:
        raise ContinuityConflict(str(missing)) from missing
    retire_entry(
        conn, **common, entry_id=entry_id,
        reason=f"already at {destination}", source_ref=source,
    )
    retired = current_entry(
        conn, common["thread_id"], common["history_epoch"], entry_id,
    )
    if retired is None:
        raise ContinuityError(f"continuity entry {entry_id} does not exist")
    set_mark(
        conn, **common, entry_id=entry_id, state="already",
        revision=int(retired["revision"]), detail=destination,
    )
    log_outcome(
        thread_id=common["thread_id"], entry_id=entry_id, revision=revision,
        outcome="already", reason="already-in-bootloader-fork",
        destination=destination, ref=source,
    )
