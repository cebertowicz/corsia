"""The one background worker that places lessons a checkpoint selected.

Its queue is the continuity mark (``infra.placement_marks``): an installed
checkpoint marks entries ``selected`` together with the fork's answer of where
each goes (``core.placement_answer``), and a poke after install or at startup
drains them. The worker makes no model call: the decision was the fork's, on
the thread's own model and effort. A lost poke only delays work, because the
next install or restart finds the same marks. One drain runs per process and
visits each entry at most once, so a deferral waits for the next wake instead
of spinning.

Every entry ends in exactly one logged outcome with its own reason:

* placed: ``written`` (committed, entry retired; a trial lesson is written with
  its owner's review), ``already`` (the destination says it, entry retired),
  ``proposed`` (a backlog issue), ``stays`` (the thread is its right home),
  ``captured`` (an incomplete hypothesis kept in a file with a review, not
  offered; the entry stays);
* ``refused``: the entry has no source at all, or a wider-audience proposal has
  no source a reviewer can open;
* unplaced (mark cleared, the next checkpoint reconsiders it): an answer that no
  longer checks out (``invalid-output``), over the cap, a verbatim proposal, no
  room that checks out (``no-room``), a fold that dropped the lesson
  (``fold-lost-lesson``), a destination that changed since the fork made room
  (``destination-changed``), Git history off, a failed commit, and any cause that can outlast every wake:
  someone else's uncommitted edits, GitHub unavailable, a worker error;
* deferred (stays ``selected``): a held lock, a destination that changed
  mid-write.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from core import placement_outcomes as outcomes
from core.placement_answer import load, thread_place
from core.placement_decide import InvalidDecision, ThreadPlace, map_destination
from infra.db_async import run_in_db, run_off_db

_task: asyncio.Task | None = None
_again = False


def poke() -> None:
    """Start a drain, or ask the running one to look again before it exits."""
    global _task, _again
    if _task is not None and not _task.done():
        _again = True
        return
    _again = False
    _task = asyncio.get_running_loop().create_task(_drain())


async def wait_idle() -> None:
    """Await the current drain; for tests and orderly shutdown."""
    if _task is not None:
        await asyncio.shield(_task)


async def _drain() -> None:
    global _again
    seen: set[tuple[str, int, int]] = set()
    while True:
        _again = False
        from infra.database import get_db
        from infra.placement_marks import selected_entries

        def queue():
            db = get_db()
            try:
                return selected_entries(db)
            finally:
                db.close()

        entries = [
            entry for entry in await run_in_db(queue)
            if (entry["thread_id"], entry["entry_id"], entry["revision"]) not in seen
        ]
        for entry in entries:
            seen.add((entry["thread_id"], entry["entry_id"], entry["revision"]))
            try:
                await _place(entry)
            except Exception:
                logger.exception(
                    "placement worker error thread={} entry={}@{}",
                    entry["thread_id"], entry["entry_id"], entry["revision"],
                )
                try:
                    await run_in_db(
                        outcomes.settle, entry, "unplaced", "worker-error",
                    )
                except Exception:
                    logger.exception("placement worker could not clear a mark")
        if not _again:
            return


async def _place(entry: dict[str, Any]) -> None:
    from infra.placement_marks import current_entry

    live = await run_in_db(_read, current_entry, entry)
    if (
        live is None or live["state"] != "active"
        or int(live["revision"]) != entry["revision"]
        or live["placement_state"] != "selected"
    ):
        outcomes.log(entry, "skipped", "entry-changed")
        return
    refs = list(entry["body"].get("source_refs") or [])
    if not refs:
        await run_in_db(
            outcomes.settle, entry, "refused", "source-missing", mark="refused",
            detail="no source ref",
        )
        return
    place = await run_in_db(_thread_place, entry["thread_id"])
    try:
        decision, room = load(entry["answer"])
        destination = map_destination(place, decision)
    except InvalidDecision:
        await run_in_db(outcomes.settle, entry, "unplaced", "invalid-output")
        return
    if destination.kind == "stays":
        await run_in_db(
            outcomes.settle, entry, "stays", destination.reason, mark="stays",
            detail=destination.reason,
        )
        return
    if destination.kind == "proposal":
        await run_in_db(outcomes.propose, entry, place, decision)
        return
    if destination.kind == "trial":
        from core.lesson_outcomes import place_trial

        await run_off_db(place_trial, entry, place, decision, destination)
        return
    await run_off_db(outcomes.write, entry, place, decision, destination, room)


def _read(reader, entry: dict[str, Any]):
    from infra.database import get_db

    db = get_db()
    try:
        return reader(db, entry["thread_id"], entry["history_epoch"], entry["entry_id"])
    finally:
        db.close()


def _thread_place(thread_id: str) -> ThreadPlace:
    from infra.database import get_db

    db = get_db()
    try:
        return thread_place(db, thread_id)
    finally:
        db.close()
