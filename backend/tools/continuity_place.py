"""``continuity_edit place``: record or queue a lesson the agent placed in-turn."""

from __future__ import annotations

import json
from typing import Any

from infra.continuity_store import ContinuityError, retire_entry


def _place(db, thread_id: str, epoch: int, params: dict[str, Any]):
    """Mark a lesson the agent placed in-turn, so no checkpoint re-reviews it.

    ``written`` with a ``destination`` and ``already`` retire the entry only
    once the destination is read and found to carry the quoted line (#2479).
    ``written`` with a ``placement`` hands it to the worker, which writes the
    lesson line and retires the entry. ``proposed`` hands the placement answer
    to the worker, which files the proposal on the project's intake route and
    marks the entry ``proposed`` itself. ``trial`` hands it the same way: the
    worker writes the trial skill and opens its review.
    """
    from core.placement_receipt import verify
    from infra.placement_marks import current_entry, log_outcome, set_mark

    entry_id = int(params["entry_id"])
    verdict = str(params["verdict"])
    if _queued(params):
        return _queue(db, thread_id, epoch, entry_id, params["placement"], verdict)
    destination = verify(db, thread_id, params["destination"], params["quote"])
    retire_entry(
        db, thread_id=thread_id, history_epoch=epoch, entry_id=entry_id,
        reason=f"placed at {destination}", source_ref=f"placed:{destination}",
    )
    revision = int(current_entry(db, thread_id, epoch, entry_id)["revision"])
    set_mark(
        db, thread_id=thread_id, history_epoch=epoch, entry_id=entry_id,
        state=verdict, revision=revision, detail=destination,
    )
    log_outcome(
        thread_id=thread_id, entry_id=entry_id, revision=revision,
        outcome=verdict, reason="placed-in-turn", destination=destination,
    )
    return {"operation": "place", "entry_id": entry_id, "verdict": verdict,
            "destination": destination}


#: The ``place`` verdicts the worker carries out, and the destination each takes.
_QUEUED_VERDICTS = {"proposed": "proposal", "trial": "trial", "written": "written"}


def _queued(params: dict[str, Any]) -> bool:
    """Whether the worker carries this ``place`` out: ``written`` does only with a ``placement``."""
    verdict = params["verdict"]
    return verdict in ("proposed", "trial") or (verdict == "written" and params.get("placement") is not None)


def _queue(db, thread_id: str, epoch: int, entry_id: int, placement: Any, verdict: str):
    """Select the entry with its answer, as a checkpoint review would (#2479).

    The worker's queue is the ``selected`` mark, so an in-turn proposal or
    trial takes the same route as a checkpoint's: checked here, carried out by
    the worker.
    """
    from core.placement_answer import stage
    from infra.placement_marks import (
        OUTSTANDING_CAP,
        current_entry,
        log_outcome,
        outstanding_count,
        set_mark,
    )

    live = current_entry(db, thread_id, epoch, entry_id)
    if live is None or live["state"] != "active":
        raise ContinuityError(f"continuity entry {entry_id} is not active")
    entry = {"entry_id": entry_id, "history_epoch": epoch, "revision": int(live["revision"]), "body": json.loads(live["body"])}
    stored = stage(db, thread_id, entry, placement, None, require=_QUEUED_VERDICTS[verdict])
    if outstanding_count(db) >= OUTSTANDING_CAP:
        raise ContinuityError(
            "the placement queue is full; keep the entry, and a later "
            "checkpoint review offers it again"
        )
    revision = int(live["revision"])
    set_mark(
        db, thread_id=thread_id, history_epoch=epoch, entry_id=entry_id,
        state="selected", revision=revision,
        detail=json.dumps(
            {"checkpoint": None, "answer": stored},
            ensure_ascii=False, sort_keys=True,
        ),
    )
    log_outcome(
        thread_id=thread_id, entry_id=entry_id, revision=revision,
        outcome="selected", reason=f"{verdict}-in-turn",
    )
    return {"operation": "place", "entry_id": entry_id, "verdict": verdict,
            "hold": stored.get("hold"), "placement": stored["placement"]}


def _trial_text(result: dict[str, Any], *, staged: bool) -> str:
    """What a trial answer becomes, and for a held one the exact way to offer it."""
    from core.lesson_admission import FIXES
    from core.lesson_files import HOLD_REASONS

    entry_id, hold = result["entry_id"], result.get("hold")
    lead = f"Staged entry {entry_id} verdict select" if staged else f"Entry {entry_id} queued"
    if hold is None:
        answer = result["placement"]
        return (
            f"{lead}: CORSIA writes the trial skill {answer['skill_name']}, opens your "
            f"review task for {answer['trial']['review_after'][:10]} and retires the "
            "entry. It is not in this conversation's skills list; it appears when the "
            "list is next rebuilt."
        )
    return (
        f"{lead} as a captured hypothesis, not offered as guidance: {HOLD_REASONS[hold]}. "
        "CORSIA keeps it in a file with a review task and the entry stays. To offer "
        f"it, {FIXES[hold]}, then call place with verdict trial for entry {entry_id}."
    )


def _place_error(params: dict[str, Any]) -> str | None:
    """``written`` takes a placement or the file and its line; ``already`` the file; the others the answer."""
    verdict = params["verdict"]
    named = params.get("destination") is not None or params.get("quote") is not None
    if verdict == "written" and named and params.get("placement") is not None:
        return "Error: place written takes a placement, or a destination and quote, not both"
    if verdict in ("proposed", "trial") or _queued(params):
        if named:
            return "Error: destination and quote go only with verdict written or already"
        if params.get("placement") is None:
            return f"Error: verdict {verdict} requires placement"
        return None
    if params.get("placement") is not None:
        return "Error: placement goes only with verdict written, proposed or trial"
    if not str(params.get("destination") or "").strip():
        return "Error: place requires destination"
    if not str(params.get("quote") or "").strip():
        return "Error: place requires quote, the line in destination that carries the lesson"
    return None
