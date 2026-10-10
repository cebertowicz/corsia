"""The checkpoint fork's placement review: which lessons deserve a durable home.

The fork that already runs at every checkpoint is the one reader that has the
whole conversation in view, so it decides which lessons to place (user
decision, 2026-09-24; durable-write requirements §5a step 1) and where each
selected one goes, on the thread's own model and effort and off its warm
prefix. This module only builds the bounded review block appended to its
instruction, after the prefix, so the cache hit survives; the fork answers
through ``continuity_edit(operation="review")``, staged like every other fork
edit and applied only if the checkpoint installs.
"""

from __future__ import annotations

import json
import sqlite3

from loguru import logger

from core.placement_decide import GUIDE

_INSTRUCTION = """[CORSIA placement review]
Each lesson below has no placement mark yet. For each one, call continuity_edit
once with operation "review", its entry_id and one verdict:
- "reaffirm": it belongs in continuity for now.
- "already": your delivered bootloader or skills already say it; pass
  destination naming that file and quote, the line in it that says it,
  copied exactly. The entry is retired once the file is found to contain it.
- "select": it should outlive this thread's continuity. Pass placement, your
  answer to the questions below; CORSIA writes it after this checkpoint
  installs. If the line would pass its file's cap, the call is refused with the
  numbered file: call again with the same placement and room.
Retire a lesson that is no longer worth keeping with drop, as usual.

Placement questions (select only):
"""


def _skip(checkpoint_id: str, thread_id: str, reason: str) -> None:
    logger.info(
        "placement.review_skipped thread={} checkpoint={} reason={}",
        thread_id, checkpoint_id, reason,
    )


def review_block(
    checkpoint_id: str, thread_id: str, *, db: sqlite3.Connection | None = None
) -> str | None:
    """Return the review block for this checkpoint, or None with a logged reason."""
    if db is None:
        from infra.database import get_db

        connection = get_db()
    else:
        connection = db
    try:
        lessons = _lessons(connection, checkpoint_id, thread_id)
        if lessons is None:
            return None
        from core.placement_answer import thread_place
        from core.placement_decide import destination_texts

        place = thread_place(connection, thread_id)
        where = {
            "thread": {"title": place.title, "persistent_agent": place.agent},
            "destinations": destination_texts(place),
        }
    finally:
        if db is None:
            connection.close()
    logger.info(
        "placement.review_offered thread={} checkpoint={} entries={}",
        thread_id, checkpoint_id, len(lessons),
    )
    return (
        _INSTRUCTION + GUIDE + "\n\nWhere it can go:\n"
        + json.dumps(where, ensure_ascii=False, sort_keys=True)
        + "\n\nLessons:\n" + json.dumps(lessons, ensure_ascii=False, sort_keys=True)
    )


def _lessons(
    connection: sqlite3.Connection, checkpoint_id: str, thread_id: str
) -> list[dict] | None:
    """The lessons to offer, each with its earlier history; None when skipped."""
    from core.placement_answer import history
    from infra.continuity_staging import _base_entries, _checkpoint
    from infra.placement_marks import (
        OUTSTANDING_CAP,
        outstanding_count,
        review_candidates,
    )

    thread = connection.execute(
        "SELECT spawned_by FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    if thread is None or thread["spawned_by"]:
        _skip(checkpoint_id, thread_id, "child-thread")
        return None
    if outstanding_count(connection) >= OUTSTANDING_CAP:
        _skip(checkpoint_id, thread_id, "deferred-at-capacity")
        return None
    checkpoint = _checkpoint(connection, checkpoint_id, thread_id)
    frozen = list(_base_entries(checkpoint).values())
    candidates = review_candidates(
        connection, thread_id, int(checkpoint["history_epoch"]), frozen,
    )
    if not candidates:
        _skip(checkpoint_id, thread_id, "batch-empty")
        return None
    lessons = []
    for entry in candidates:
        lesson = {"entry_id": int(entry["entry_id"]), "field": entry["field"], **entry["body"]}
        lessons.append({**lesson, "earlier_history": history(connection, thread_id, lesson)})
    return lessons
