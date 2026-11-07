"""What happens to one selected lesson once its destination is known.

Each function here ends the entry in exactly one logged outcome (see
``core.placement_worker`` for the list): it writes and commits, files a
proposal, or records why the lesson stays where it is.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from core.placement_decide import Decision, Destination, ThreadPlace
from core.placement_plan import ALREADY, commit_plan, relative_path
from core.placement_room import Operation, Room
from infra.placement_marks import log_outcome


def log(entry: dict[str, Any], outcome: str, reason: str, **extra: str) -> None:
    """Log one outcome line for this entry without changing its mark."""
    log_outcome(
        thread_id=entry["thread_id"], entry_id=entry["entry_id"],
        revision=entry["revision"], outcome=outcome, reason=reason, **extra,
    )


#: Deferrals that clear by the worker's next wake, so the entry keeps its
#: selection. Any other cause can outlast every wake (someone's uncommitted
#: edit, GitHub not signed in), so it ends the selection with its own reason
#: rather than holding a place under the outstanding cap.
_TRANSIENT = frozenset({
    "deferred-guidance-lock", "deferred-index-locked", "deferred-destination-changed",
})


def defer(entry: dict[str, Any], reason: str, destination: str = "") -> None:
    """Keep the selection for a transient cause; otherwise leave it unplaced."""
    if reason in _TRANSIENT:
        log(entry, "deferred", reason, destination=destination)
        return
    settle(
        entry, "unplaced", reason.removeprefix("deferred-"), destination=destination,
    )


def settle(
    entry: dict[str, Any],
    outcome: str,
    reason: str,
    *,
    mark: str | None = None,
    detail: str | None = None,
    retire: str | None = None,
    source_ref: str | None = None,
    destination: str = "",
) -> None:
    """Record one final outcome: a mark (or cleared mark) and a log line."""
    from infra.continuity_store import retire_entry
    from infra.database import get_db, write_transaction
    from infra.placement_marks import clear_mark, current_entry, set_mark

    common = {
        "thread_id": entry["thread_id"],
        "history_epoch": entry["history_epoch"],
        "entry_id": entry["entry_id"],
    }
    db = get_db()
    try:
        with write_transaction(db, scope="placement_outcome") as connection:
            key = (entry["thread_id"], entry["history_epoch"], entry["entry_id"])
            live = current_entry(connection, *key)
            if live is None or int(live["revision"]) != entry["revision"]:
                log(entry, "skipped", "entry-changed")
                return
            revision = entry["revision"]
            if retire is not None:
                retire_entry(
                    connection, **common, reason=retire,
                    source_ref=source_ref or detail,
                )
                revision = int(current_entry(connection, *key)["revision"])
            if mark is None:
                clear_mark(connection, **common)
            else:
                set_mark(
                    connection, **common, state=mark, revision=revision,
                    detail=detail,
                )
    finally:
        db.close()
    log(entry, outcome, reason, destination=destination, ref=detail or "")


def entry_text(entry: dict[str, Any]) -> str:
    """The entry's own words, for the verbatim check; one field per line."""
    body = entry["body"]
    return "\n".join(
        str(body.get(key) or "")
        for key in ("statement", "rationale", "result", "evidence")
    )


def refuse(entry: dict[str, Any], relative: str, errors: tuple[str, ...]) -> None:
    """Leave an invalid-skill placement reconsiderable: one ``unplaced`` line, the errors as its ref."""
    from infra.skill_save_validation import INVALID_SKILL_REASON

    settle(entry, "unplaced", INVALID_SKILL_REASON, detail="; ".join(errors), destination=relative)


def write(
    entry: dict[str, Any],
    place: ThreadPlace,
    decision: Decision,
    destination: Destination,
    room: Room | None = None,
) -> None:
    """Write one lesson to its file as one commit, then retire the entry.

    Over the cap, ``room`` (the make-room answer) must fit it in the same
    commit; without one, or when it does not check out, nothing is written.
    """
    from core.placement_save import save_placement

    path = destination.path
    assert path is not None
    relative = relative_path(path)
    planned = commit_plan(place, decision, destination, room)
    if planned == ALREADY:
        settle(
            entry, "already", ALREADY, mark="already",
            detail=relative, retire=f"already at {relative}",
            source_ref=f"file:{relative}", destination=relative,
        )
        return
    if isinstance(planned, str):
        settle(entry, "unplaced", planned, destination=relative)
        return
    writes, expected, operations = planned
    sha = save_placement(
        entry, place, decision, relative, path, writes, expected, operations,
        settle=settle, defer=defer, refuse=refuse, author=_author, message=_message, logger=logger,
    )
    if sha is None:
        return
    settle(
        entry, "written", "written", mark="written",
        detail=f"{relative} ({sha[:12]})", retire=f"placed at {relative} ({sha[:12]})",
        source_ref=f"commit:{sha}", destination=relative,
    )


def _author(place: ThreadPlace) -> str:
    name = re.sub(r"[<>\n]", "", place.title).strip() or place.thread_id
    return f"{name} <{place.thread_id}@agents.corsia.local>"


def _message(
    entry: dict[str, Any],
    decision: Decision,
    relative: str,
    trailer: str,
    operations: list[Operation] | None = None,
    verb: str = "written",
) -> str:
    """Commit message: why, where and each make-room step, never the lesson (P3)."""
    from core.placement_proposal import p3_violation, thread_user_names
    from infra.database import get_db

    db = get_db()
    try:
        names = thread_user_names(db, entry["thread_id"])
    finally:
        db.close()
    reason = decision.rationale
    if p3_violation(reason, entry_text(entry), names):
        reason = f"{decision.q1} lesson needed {decision.q2.replace('_', ' ')}"
    checkpoint = str(entry.get("checkpoint") or "").removeprefix("checkpoint:")
    room = "".join(f"Room: {op.describe()}\n" for op in operations or [])
    return (
        f"placement: {verb} {relative}\n\n"
        f"Reason: {reason}\n"
        f"{room}"
        f"Evidence: checkpoint {checkpoint or '-'} entry "
        f"{entry['entry_id']}@{entry['revision']}\n"
        f"{trailer}\n"
        f"Scope: {decision.q1}\n"
    )


def propose(entry: dict[str, Any], place: ThreadPlace, decision: Decision) -> None:
    """File one project or workspace lesson as a backlog issue."""
    from core.placement_proposal import (
        backlog_target,
        file_issue,
        p3_violation,
        resolvable_ref,
        thread_user_names,
    )
    from infra.database import get_db

    db = get_db()
    try:
        resolvable = resolvable_ref(
            db, entry["thread_id"], list(entry["body"].get("source_refs") or []),
        )
        names = thread_user_names(db, entry["thread_id"])
        label, repo = backlog_target(db, place.project_id)
    finally:
        db.close()
    if not resolvable:
        settle(
            entry, "refused", "source-unresolved", mark="refused",
            detail="no openable source for a wider audience",
        )
        return
    if p3_violation(decision.text, entry_text(entry), names) or p3_violation(
        decision.rationale, entry_text(entry), names
    ):
        settle(entry, "unplaced", "p3-verbatim")
        return
    if label is None or repo is None:
        settle(entry, "unplaced", "no-backlog-target")
        return
    result = file_issue(
        repo=repo, label=label, thread_id=entry["thread_id"],
        entry_id=entry["entry_id"], revision=entry["revision"],
        q1=decision.q1, q2=decision.q2, rule=decision.text,
        rationale=decision.rationale,
    )
    if result.outcome == "deferred":
        defer(entry, result.reason)
        return
    settle(
        entry, "proposed", result.reason, mark="proposed",
        detail=result.url or repo, destination=repo,
    )
