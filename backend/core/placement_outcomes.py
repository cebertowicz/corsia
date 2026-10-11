"""What happens to one selected lesson once its destination is known.

Each function here ends the entry in exactly one logged outcome (see
``core.placement_worker`` for the list): it writes and commits, files a
proposal, or records why the lesson stays where it is.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

from loguru import logger

from core.placement_decide import (
    SKILL_DESCRIPTION_WORDS,
    Decision,
    Destination,
    ThreadPlace,
    append_lesson,
    append_to_reference,
    append_to_skill,
    over_cap,
    says,
    words,
)
from core import placement_room
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


def _today() -> str:
    """The lesson date, in the local time of the host the user runs CORSIA on."""
    return date.today().isoformat()


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
    relative = _relative(path)
    planned = commit_plan(place, decision, destination, room)
    if planned == _ALREADY:
        settle(
            entry, "already", _ALREADY, mark="already",
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


def room_request(
    place: ThreadPlace, decision: Decision, destination: Destination
) -> tuple[str, str] | None:
    """The base text and numbered-file request when this write would pass its cap."""
    path = destination.path
    if path is None or destination.kind not in ("context", "skill") or not path.exists():
        return None
    current = path.read_text()
    if decision.duplicate_of and says(current, decision.duplicate_of):
        return None
    if _over_cap(destination.kind, current, decision) is None:
        return None
    overflow = placement_room.overflow_file(destination.kind, path, place.reference_dir)
    after = _new_text(destination.kind, current, decision)
    return current, placement_room.build_prompt(
        current, decision.text, placement_room.need(destination.kind, after),
        _relative(overflow) if overflow is not None else None,
    )


def room_refusal(
    place: ThreadPlace, decision: Decision, destination: Destination, room: Room
) -> str | None:
    """Why ``room`` cannot make room in its base text, or None when it can.

    The same check the write runs, applied to the text the fork was shown,
    so a room that can never apply goes back to the fork instead of settling
    ``no-room`` after it is gone.
    """
    made = _made_room(place, decision, destination, room.base, room)
    return made if isinstance(made, str) else None


_ALREADY = "already-at-destination"
_Plan = tuple[dict[Path, str], dict[Path, str | None], list[Operation]]


def commit_plan(
    place: ThreadPlace, decision: Decision, destination: Destination, room: Room | None,
) -> _Plan | str:
    """The one commit plan for this answer, or why it writes nothing.

    ``write`` commits it and the answer-time loadability check validates it,
    so both see the same bytes. ``_ALREADY`` when the file already says the
    lesson; otherwise the ``unplaced`` reason.
    """
    from infra.guidance_git import digest

    path = destination.path
    assert path is not None
    current = path.read_text() if path.exists() else None
    if decision.duplicate_of and current is not None and says(current, decision.duplicate_of):
        return _ALREADY
    over = _over_cap(destination.kind, current, decision)
    if over is None:
        return {path: _new_text(destination.kind, current, decision)}, {path: digest(current)}, []
    if room is None:
        return over
    if current != room.base:
        # The fork made room against text that is gone; only a fresh
        # review can make room again, so the next checkpoint reconsiders.
        return "destination-changed"
    return _made_room(place, decision, destination, current, room)


def _made_room(
    place: ThreadPlace,
    decision: Decision,
    destination: Destination,
    current: str | None,
    room: Room,
) -> tuple[dict[Path, str], dict[Path, str | None], list[Operation]] | str:
    """The commit's files and expected digests after making room, or why not.

    ``no-room`` when the operations do not check out; ``fold-lost-lesson`` when
    a fold replaced the appended lesson but the file does not say it.
    """
    from infra.config import WORKSPACE
    from infra.guidance_git import digest

    path = destination.path
    if current is None or path is None:
        return "no-room"
    overflow = placement_room.overflow_file(destination.kind, path, place.reference_dir)
    overflow_current = (
        overflow.read_text() if overflow is not None and overflow.exists() else None
    )
    applied = placement_room.apply(
        room, kind=destination.kind, current=current,
        add_lesson=lambda text: _new_text(destination.kind, text, decision),
        workspace=Path(WORKSPACE), destination=path, overflow=overflow,
        overflow_current=overflow_current, day=_today(),
    )
    if applied is None:
        return "no-room"
    text, overflow_text, operations = applied
    if any(op.op == "fold" for op in operations) and not says(text, decision.text):
        return "fold-lost-lesson"
    writes, expected = {path: text}, {path: digest(current)}
    if overflow is not None and overflow_text is not None:
        writes[overflow] = overflow_text
        expected[overflow] = digest(overflow_current)
    return writes, expected, operations


def _over_cap(kind: str, current: str | None, decision: Decision) -> str | None:
    """The reason a write would pass its WORKSPACE-STANDARD cap, or None."""
    if kind not in ("context", "skill"):
        return None
    if over_cap(kind, _new_text(kind, current, decision)):
        return "over-cap-unplaced"
    description = decision.skill_description or decision.text
    if kind == "skill" and current is None and words(description) > SKILL_DESCRIPTION_WORDS:
        return "over-cap-unplaced"
    return None


def _new_text(kind: str, current: str | None, decision: Decision) -> str:
    if kind == "context":
        return append_lesson(current, decision.text, _today())[0]
    if kind == "skill":
        return append_to_skill(
            current, str(decision.skill_name), decision.text,
            " ".join((decision.skill_description or "").split()) or None,
        )
    return append_to_reference(current or "", decision.text)


def _relative(path: Path) -> str:
    from infra.config import WORKSPACE

    try:
        return str(path.resolve().relative_to(Path(WORKSPACE).resolve()))
    except ValueError:
        return str(path)


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
