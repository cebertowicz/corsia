"""The checkpoint fork's placement answer: checked when staged, read back later.

Where a selected lesson goes is decided by the checkpoint fork itself, on the
thread's own model and effort and off its warm prefix, so the decision costs no
separate model call (user decision, 2026-09-24). The fork answers
``continuity_edit(operation="review", verdict="select", placement=...)``. This
module checks that answer when it is staged, so a bad one goes back to the fork
while it can still correct it, and when the write would pass its cap it refuses
with the numbered file so the fork adds ``room`` in the same tool loop. The
worker (``core.placement_worker``) then reads the stored answer back and acts on
it mechanically.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from core import placement_room
from core.placement_decide import (
    Decision,
    Destination,
    InvalidDecision,
    ThreadPlace,
    map_destination,
    validate_decision,
)
from core.placement_room import Room

_HISTORY_HITS = 3

#: What an in-turn ``place`` verdict's answer must map to, and what to do otherwise.
_REQUIRED = {
    "proposal": (
        "verdict proposed is for a project or everywhere lesson; a thread "
        "or agent lesson is yours to write: write it, then place written"
    ),
    "trial": (
        "verdict trial takes a placement with trial set and q1 agent, in a "
        "thread that has its own tray"
    ),
    "written": (
        "verdict written takes a q1 agent placement in a thread with its own tray: a "
        "project or everywhere lesson is proposed, a hypothesis is a trial, and a "
        "thread lesson stays in continuity"
    ),
}

_AT_CAP = "that file is at its cap: retire or shorten a line there you no longer need, then place again"

#: The destination kinds each in-turn verdict's placement may map to.
_KINDS = {
    "proposal": ("proposal",), "trial": ("trial",), "written": ("context", "skill", "reference"),
}


def thread_place(db: sqlite3.Connection, thread_id: str) -> ThreadPlace:
    """What placement knows about a thread: its tray and agent reference."""
    from core.context_source_paths import _thread_scope
    from infra.config import WORKSPACE

    row = db.execute(
        "SELECT title,project_id,agent_enabled,agent_paths FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    project_id, tray = _thread_scope(thread_id, Path(WORKSPACE), db)
    reference = None
    if tray is not None and row is not None and row["agent_enabled"]:
        try:
            paths = json.loads(row["agent_paths"] or "[]")
        except (TypeError, ValueError):
            paths = []
        scope_root = tray.parent.parent
        for candidate in paths if isinstance(paths, list) else []:
            text = str(candidate).strip().rstrip("/")
            if text.startswith("shared/") and ".." not in text.split("/"):
                if (scope_root / text).is_dir():
                    reference = scope_root / text
                    break
    return ThreadPlace(
        thread_id=thread_id,
        title=str(row["title"] or thread_id) if row is not None else thread_id,
        project_id=project_id,
        agent=bool(row["agent_enabled"]) if row is not None else False,
        tray=tray,
        reference_dir=reference,
    )


def history(db: sqlite3.Connection, thread_id: str, entry: dict[str, Any]) -> list[dict]:
    """Earlier decisions on the same subject, for the duplicate check (P5)."""
    from memory.checkpoint_search import search_checkpoints

    query = str(entry.get("statement") or entry.get("result") or "")[:300]
    hits = search_checkpoints(
        db, query, caller_thread_id=thread_id, limit=_HISTORY_HITS + 1,
    )
    return [
        {
            "field": hit.get("field"),
            "statement": hit.get("statement"),
            "state": hit.get("entry_state"),
            "retired_reason": hit.get("retired_reason"),
        }
        for hit in hits
        if hit.get("kind") == "continuity_entry"
        and not (hit.get("thread_id") == thread_id
                 and hit.get("entry_id") == entry.get("entry_id"))
    ][:_HISTORY_HITS]


def stage(
    db: sqlite3.Connection,
    thread_id: str,
    entry: dict[str, Any],
    placement: object,
    room: object,
    *,
    require: str | None = None,
) -> dict[str, Any]:
    """Check one ``select`` answer for ``entry`` and return what the mark stores.

    ``require`` names the one destination kind an in-turn ``place`` verdict
    takes (``proposal`` or ``trial``) and refuses an answer that maps elsewhere.
    A trial answer also stores ``hold``: why it will be captured rather than
    offered, or None.

    Raises ``ContinuityError`` with the reason the fork must correct, including
    a proposal's text or rationale that the worker's name screen would refuse,
    the numbered file when the write would pass its cap and no ``room`` came,
    or when the ``room`` that came cannot apply to that file, and a skill
    write the loader would refuse.
    """
    from core.placement_outcomes import room_refusal, room_request
    from infra.continuity_store import ContinuityError

    entry_id = int(entry["entry_id"])
    try:
        decision = replace(
            validate_decision(placement),
            originating_subjects=_originating_subjects(db, thread_id),
        )
        place = thread_place(db, thread_id)
        destination = map_destination(place, decision)
        if require is not None and destination.kind not in _KINDS[require]:
            raise InvalidDecision(_REQUIRED[require])
        if destination.kind == "trial":
            return _stage_trial(db, thread_id, entry, place, decision, destination, room)
        if destination.kind == "proposal":
            _check_publishable(db, thread_id, entry, decision)
        request = room_request(place, decision, destination)
        if request is not None and require is not None:
            raise InvalidDecision(_AT_CAP)
        if request is None:
            if room is not None:
                raise InvalidDecision(
                    "room is only for a write that would pass its cap; "
                    "call again without room"
                )
            _check_loadable(place, decision, destination, None, entry_id)
            return {"placement": _stored(decision), "room": None}
        base, prompt = request
        if room is None:
            raise InvalidDecision(
                f"this line would pass its destination's cap. Call review again "
                f"for entry {entry_id} with the same placement and room.\n\n"
                f"{placement_room.GUIDE}\n\n{prompt}"
            )
        operations = placement_room.validate_operations({"operations": room})
        # An empty room is the fork's honest "nothing makes room": it stays
        # staged and the worker settles it as a logged ``no-room``.
        refused = operations and room_refusal(
            place, decision, destination, Room(base=base, operations=operations),
        )
        if refused:
            raise InvalidDecision(
                f"this room cannot apply to the numbered file ({refused}). Call "
                f"review again for entry {entry_id} with the same placement and "
                f"corrected room.\n\n{placement_room.GUIDE}\n\n{prompt}"
            )
        if operations:
            _check_loadable(
                place, decision, destination,
                Room(base=base, operations=operations), entry_id,
            )
    except InvalidDecision as invalid:
        raise ContinuityError(str(invalid)) from invalid
    return {
        "placement": _stored(decision),
        "room": {"base": base, "operations": room},
    }


def _originating_subjects(db: sqlite3.Connection, thread_id: str) -> tuple[str, ...]:
    """Delegate active-turn subject discovery to the lesson subsystem."""
    from core.lesson_corrections import current_owner_subjects

    return current_owner_subjects(db, thread_id)


def _stored(decision: Decision) -> dict[str, Any]:
    """The answer as the mark keeps it; an answer without a trial is unchanged."""
    stored = asdict(decision)
    if stored["trial"] is None:
        del stored["trial"]
    if not stored["originating_subjects"]:
        del stored["originating_subjects"]
    return stored


def _check_loadable(
    place: ThreadPlace, decision: Decision, destination: Destination,
    room: Room | None, entry_id: int,
) -> None:
    """Refuse now a skill write the worker would settle ``unplaced/invalid-skill``.

    The same prevalidation ``save_placement`` runs, so a write the loader
    would reject goes back to the fork instead of being reselected at every
    checkpoint.
    """
    from core.placement_outcomes import commit_plan
    from infra.skill_save_validation import invalid_skill_write

    if destination.kind != "skill" or destination.path is None:
        return
    planned = commit_plan(place, decision, destination, room)
    if isinstance(planned, str):
        return
    refusal = invalid_skill_write({path: text.encode() for path, text in planned[0].items()})
    if refusal is not None:
        raise InvalidDecision(
            f"the skill would not load after this write ({'; '.join(refusal.errors)}). "
            f"Call review again for entry {entry_id} with another destination "
            "or a line that keeps it valid"
        )


def _stage_trial(
    db: sqlite3.Connection, thread_id: str, entry: dict[str, Any], place: ThreadPlace,
    decision: Decision, destination: Destination, room: object,
) -> dict[str, Any]:
    """Refuse what the worker would leave unplaced; say now what it will hold."""
    from core.lesson_admission import admit

    if room is not None:
        raise InvalidDecision("room does not go with a trial; call again without room")
    from core.lesson_corrections import incident_detail_flags, record_placement_check
    from core.placement_outcomes import entry_text
    from core.placement_proposal import thread_user_names

    admission = admit(db, thread_id, entry, place, decision, destination)
    flags = incident_detail_flags(decision.text, entry_text(entry), thread_user_names(db, thread_id))
    if flags:
        record_placement_check(
            db, thread_id=thread_id, history_epoch=int(entry["history_epoch"]),
            entry_id=int(entry["entry_id"]), revision=int(entry.get("revision") or 0), flags=flags,
            override_reason=decision.trial.incident_detail_reason if decision.trial else None,
        )
    return {
        "placement": _stored(decision), "room": None, "hold": admission.hold,
        "placement_check": {
            "flags": list(flags),
            "override_reason": decision.trial.incident_detail_reason if decision.trial else None,
        },
    }


def _check_publishable(
    db: sqlite3.Connection, thread_id: str, entry: dict[str, Any], decision: Decision
) -> None:
    """Refuse now what the worker would settle ``unplaced/p3-verbatim`` later.

    The worker runs the same screen before filing; refused here, the author
    rephrases in the same call instead of the lesson silently going nowhere.
    """
    from core.placement_outcomes import entry_text
    from core.placement_proposal import p3_problem, thread_user_names

    names = thread_user_names(db, thread_id)
    source = entry_text(entry)
    for part, text in (("text", decision.text), ("rationale", decision.rationale)):
        problem = p3_problem(text, source, names)
        if problem:
            raise InvalidDecision(
                f"a proposal is published, and its {part} may not name a person "
                f"or copy the lesson: {problem}. Rephrase it as an impersonal "
                "rule and call again"
            )


def load(answer: dict[str, Any]) -> tuple[Decision, Room | None]:
    """The stored answer as the worker acts on it; ``InvalidDecision`` if malformed."""
    raw = answer.get("placement")
    if not isinstance(raw, dict):
        raise InvalidDecision("stored placement must be an object")
    raw = dict(raw)
    subjects = raw.pop("originating_subjects", [])
    from core.lesson_corrections import merge_subjects

    if not isinstance(subjects, list):
        raise InvalidDecision("stored originating_subjects must be a list")
    decision = replace(validate_decision(raw), originating_subjects=merge_subjects(subjects))
    room = answer.get("room")
    if room is None:
        return decision, None
    if not isinstance(room, dict) or not isinstance(room.get("base"), str):
        raise InvalidDecision("stored room must carry its base text")
    operations = placement_room.validate_operations(
        {"operations": room.get("operations")},
    )
    return decision, Room(base=room["base"], operations=operations)
