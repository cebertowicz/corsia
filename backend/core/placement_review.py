"""The checkpoint fork's review of saved entries: write, retire or justify each.

The fork that already runs at every checkpoint is the one reader that has the
whole conversation in view, so it decides what each due entry becomes (user
decision, 2026-09-24; durable-write requirements §5a step 1; rotating review,
2026-10-09): written where its reader looks, retired, or kept with a reason.
This module builds the bounded review block appended to its instruction, after
the prefix, so the cache hit survives, and admits it into the fork only when it
fits. The fork answers through ``continuity_edit(operation="review")``, staged
like every other fork edit and applied only if the checkpoint installs.

The block never makes a checkpoint fail: any error, or a block the fork has no
room for, drops the review with a logged reason and the checkpoint goes ahead.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import replace
from typing import Any

from loguru import logger

from core.placement_decide import GUIDE

_HEAD = """[CORSIA saved-entry review]
Nothing lives in continuity permanently: each entry below is due, so write it
where its reader looks, retire it, or say why it still belongs here. For each
one, call continuity_edit once:
"""

_WRITE = """- operation "review", verdict "select": write it to a file. Pass placement,
  your answer to the questions below; CORSIA writes it after this checkpoint
  installs and retires the entry. If the line would pass its file's cap, the
  call is refused with the numbered file: call again with the same placement
  and room.
"""

_THE_REST = """- operation "review", verdict "already": your delivered bootloader or skills
  already say it; pass destination naming that file and quote, the line in it
  that says it, copied exactly. The entry is retired once the file is found to
  contain it.
- operation "review", verdict "reaffirm", with reason: it still belongs in
  continuity, and the reason says why (a freeze with an end date, say). The
  reason is shown the next time the entry is offered. Not for a lesson.
- operation "drop", with reason: it is finished, superseded or no longer true.
  A constraint, approval or decision also needs source_ref, the evidence.

By kind (the offered entry names its kind):
- lesson (decisions, rejected_alternatives, known_failures): never kept. Select
  it, or say it is already on disk, or drop it. A lesson for only this thread's
  work goes to this thread's own file: answer q1 agent, not thread.
- user_rule (approvals_and_preferences, active_constraints): select it into the
  bootloader or skill where its audience reads it, keeping the user's own words;
  drop it with the evidence that supersedes it; or reaffirm it with a reason.
- open_item (blockers, open_questions): drop it when resolved, or reaffirm it
  with a reason.
- work_state (facts, verification, relevant_state, recently_completed,
  hypotheses): select it into the domain reference or a skill, drop it when it
  is done or stale, or reaffirm it with a reason.
- next_actions: a step still to do stays until it is done: reaffirm it, with
  the reason. Dropping it loses it, because this review cannot add it to the
  plan. Drop only a step that is done or stale. Record no new next step here:
  the step in progress and the next action go in your narrative.
An entry you leave alone is offered again at the next checkpoint.
"""

_NO_WRITES = """The write queue is full, so verdict select is unavailable at this
checkpoint. Entries that need writing stay offered; give the rest their verdicts.
"""

_PLACEMENT = "\nPlacement questions (select only):\n"

#: A hit's statement is cut to this many characters in the block.
_HISTORY_CHARS = 240

#: The kinds that get the earlier-history duplicate check: those a write ends.
_CHECKED_KINDS = ("lesson", "user_rule")


def _skip(checkpoint_id: str, thread_id: str, reason: str) -> None:
    logger.info(
        "placement.review_skipped thread={} checkpoint={} reason={}",
        thread_id, checkpoint_id, reason,
    )


def review_block(
    checkpoint_id: str,
    thread_id: str,
    room_tokens: int,
    *,
    db: sqlite3.Connection | None = None,
) -> str | None:
    """Return the review block for this checkpoint, or None with a logged reason.

    ``room_tokens`` is the instruction room left for the whole block. Its fixed
    text and destination files come off it first; entries then fill the rest,
    oldest first, up to ``REVIEW_BATCH_TOKENS``, and the batch stops at the
    first entry that does not fit. With no room for even one, nothing is offered.
    """
    from core.token_estimation import _text_tokens
    from infra.placement_marks import OUTSTANDING_CAP, REVIEW_BATCH_TOKENS, outstanding_count

    if db is None:
        from infra.database import get_db

        connection = get_db()
    else:
        connection = db
    try:
        due = _due_entries(connection, checkpoint_id, thread_id)
        if due is None:
            return None
        writes = outstanding_count(connection) < OUTSTANDING_CAP
        fixed = _fixed_text(connection, thread_id, writes)
        budget = min(REVIEW_BATCH_TOKENS, room_tokens - _text_tokens(fixed, None))
        entries = _fill(connection, thread_id, due, budget)
    finally:
        if db is None:
            connection.close()
    if not entries:
        _skip(checkpoint_id, thread_id, "no-room")
        return None
    kinds = Counter(entry["kind"] for entry in entries)
    offered = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    logger.info(
        "placement.review_batch thread={} checkpoint={} entries={} tokens={} room={} kinds={}",
        thread_id, checkpoint_id, len(entries), _text_tokens(fixed + offered, None),
        room_tokens, ",".join(f"{kind}={kinds[kind]}" for kind in sorted(kinds)),
    )
    return fixed + offered


def _fixed_text(connection: sqlite3.Connection, thread_id: str, writes: bool) -> str:
    """The review text that does not depend on the entries offered."""
    head = _HEAD + (_WRITE if writes else "") + _THE_REST
    if not writes:
        return head + "\n" + _NO_WRITES + "\n\nEntries:\n"
    from core.placement_answer import thread_place
    from core.placement_decide import destination_texts

    place = thread_place(connection, thread_id)
    where = {
        "thread": {"title": place.title, "persistent_agent": place.agent},
        "destinations": destination_texts(place),
    }
    return (
        head + _PLACEMENT + GUIDE + "\n\nWhere it can go:\n"
        + json.dumps(where, ensure_ascii=False, sort_keys=True) + "\n\nEntries:\n"
    )


def _due_entries(
    connection: sqlite3.Connection, checkpoint_id: str, thread_id: str
) -> list[dict[str, Any]] | None:
    """The entries due for review, front of the queue first; None when skipped."""
    from infra.continuity_staging import _base_entries, _checkpoint
    from infra.review_queue import review_queue

    thread = connection.execute(
        "SELECT spawned_by FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    if thread is None or thread["spawned_by"]:
        _skip(checkpoint_id, thread_id, "child-thread")
        return None
    checkpoint = _checkpoint(connection, checkpoint_id, thread_id)
    frozen = list(_base_entries(checkpoint).values())
    due = review_queue(connection, thread_id, int(checkpoint["history_epoch"]), frozen)
    if not due:
        _skip(checkpoint_id, thread_id, "nothing-due")
        return None
    return due


def _offer(
    connection: sqlite3.Connection, thread_id: str, entry: dict[str, Any]
) -> dict[str, Any]:
    """One entry as the fork sees it, with its kept reason and earlier history."""
    from core.placement_answer import history
    from infra.placement_marks import FIELD_KIND

    kind = FIELD_KIND[entry["field"]]
    offer: dict[str, Any] = {
        "entry_id": int(entry["entry_id"]), "field": entry["field"],
        "kind": kind, **entry["body"],
    }
    if entry["kept_reason"]:
        offer["kept_before"] = entry["kept_reason"]
    if kind in _CHECKED_KINDS:
        offer["earlier_history"] = [
            {**hit, "statement": str(hit.get("statement") or "")[:_HISTORY_CHARS]}
            for hit in history(connection, thread_id, offer)
        ]
    return offer


def _fill(
    connection: sqlite3.Connection,
    thread_id: str,
    due: list[dict[str, Any]],
    budget_tokens: int,
) -> list[dict[str, Any]]:
    """Offers off the front of the queue while they fit; stops at the first that does not."""
    from core.token_estimation import _text_tokens

    entries: list[dict[str, Any]] = []
    spent = 0
    for entry in due:
        offer = _offer(connection, thread_id, entry)
        tokens = _text_tokens(json.dumps(offer, ensure_ascii=False, sort_keys=True), None)
        if spent + tokens > budget_tokens:
            break
        entries.append(offer)
        spent += tokens
    return entries


async def attach_review(request):
    """Append the review block to a checkpoint request's instruction, if it fits.

    The room is what the fork's window leaves for its instruction once its
    output and one tool round trip are held back (``fork_instruction_room``),
    less the instruction already there: a smaller window gets a smaller batch.
    The review is an addition to the checkpoint, never a reason to fail it: an
    error or a block with no room leaves the request as it was.
    """
    from core.checkpoint_room import fork_instruction_room
    from core.token_estimation import _text_tokens
    from infra.db_async import run_in_db

    window = request.target.context_window
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        _skip(request.checkpoint_id, request.thread_id, "no-window")
        return request
    try:
        room = fork_instruction_room(
            window, request.max_output_tokens,
        ) - _text_tokens(request.instruction, None)
        block = await run_in_db(
            review_block, request.checkpoint_id, request.thread_id, room,
        )
        if block is None:
            return request
    except Exception:
        logger.exception(
            "placement.review_skipped thread={} checkpoint={} reason=review-error",
            request.thread_id, request.checkpoint_id,
        )
        return request
    logger.info(
        "placement.review_offered thread={} checkpoint={}",
        request.thread_id, request.checkpoint_id,
    )
    return replace(request, instruction=request.instruction + "\n\n" + block)
