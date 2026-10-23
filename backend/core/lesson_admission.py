"""Whether one placement answer's trial is offered, held, or sent back.

The placement stage and the placement worker both ask here, so the reply an
agent gets and the outcome the worker records cannot disagree. A refusal goes
back to the caller to correct in the same call. A hold is not an error: the
hypothesis is kept as a captured file with a review and is not offered as
guidance. The guard checks fields, never how sure the model sounds. Nothing
here writes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import lesson_files
from core.correction_learning import CAUSES
from core.lesson_trial_targeting import (
    TrialRefused,
    additive_only as _additive_only,
    same_case as _same_case,
    target as _target,
    target_from_lesson as _target_from_lesson,
    target_trials as _target_trials,
    trial_row as _trial_row,
)
from core.lesson_trials import PERMANENT_KINDS, lesson_id, parse_instant
from core.thread_identity import thread_agent_identity
from core.placement_decide import Decision, Destination, ThreadPlace

#: A source a person wrote: citing one does not make the agent's rule theirs.
HUMAN_KINDS = frozenset({"user", "message"})

#: What turns each hold into an offered trial, as the reply tells the caller.
FIXES = {
    "missing-derivation": (
        "add trial.derivation saying how the rule follows from the person's words"
    ),
    "literal-unresolved": (
        "keep the person's exact words in their own continuity entry, or place "
        "them in their permanent home, citing the same message this entry "
        "cites; then name that in trial.literal_ref as {entry_id} or "
        "{destination, quote}"
    ),
    "no-selective-trigger": (
        "give skill_description (when it applies) and trial.boundary (when it does not)"
    ),
}



@dataclass(frozen=True)
class Admission:
    """One trial answer, checked.

    ``capture_id`` is the entry's own derived identity. ``lesson_id`` differs
    from it only when the answer revises a trial the owner already holds.
    ``text`` is the trial file without its review reference, and is None while
    ``hold`` says why the hypothesis is only captured.
    """

    lesson_id: str
    capture_id: str
    hold: str | None = None
    text: str | None = None
    destination_path: Path | None = None
    open_trials: tuple[dict[str, str], ...] = ()
    retired_matches: tuple[dict[str, str], ...] = ()
    merged_into: str | None = None


def human_refs(body: dict[str, Any]) -> set[tuple[str, str]]:
    """The things a person said that an entry body cites."""
    return {
        (str(ref.get("kind")), str(ref.get("reference")))
        for ref in body.get("source_refs") or []
        if isinstance(ref, dict) and ref.get("kind") in HUMAN_KINDS
    }


def human_cited(entry: dict[str, Any]) -> bool:
    """Whether the entry cites something a person said."""
    return bool(human_refs(entry["body"]))


def _learning_metadata(decision: Decision) -> None:
    target = decision.target_file
    if not target or not decision.cause:
        raise TrialRefused("missing-learning-metadata", "Trial admission needs target_file and cause.")
    if target == "new":
        if not decision.new_target_path:
            raise TrialRefused("missing-learning-metadata", "target_file new requires new_target_path.")
    elif decision.new_target_path:
        raise TrialRefused("invalid-learning-metadata", "new_target_path is only allowed when target_file is new.")
    if not decision.target_section or not decision.case:
        raise TrialRefused("missing-learning-metadata", "Trial admission needs target_section and case (what_happened, right_call, does_not_apply, correction_subject).")
    if decision.cause not in CAUSES:
        raise TrialRefused("invalid-learning-metadata", "cause must be a System correction cause.")
    if decision.new_evidence not in (None, "target_changed", "case_differs"):
        raise TrialRefused("invalid-learning-metadata", "new_evidence must be target_changed or case_differs.")




def admit(
    db: sqlite3.Connection, thread_id: str, entry: dict[str, Any], place: ThreadPlace,
    decision: Decision, destination: Destination, *, now: datetime | None = None,
) -> Admission:
    """Check one trial answer; ``TrialRefused`` says what the caller must correct."""
    trial = decision.trial
    assert trial is not None
    _learning_metadata(decision)
    if trial.kind in PERMANENT_KINDS:
        raise TrialRefused("permanent-kind", (
            f"a trial is only for a hypothesis. A person's {trial.kind} keeps its "
            "permanent home and never expires: call again without trial, with the "
            "q1 and q2 that say who needs it and when"
        ))
    if decision.duplicate_of and (destination.path is None or not destination.path.exists()):
        raise TrialRefused("invalid-output", (
            "duplicate_of and trial do not go together unless it names an existing "
            "watch or trial lesson; choose its skill_name or drop duplicate_of"
        ))
    due = parse_instant(trial.review_after)
    if due is None or due <= (now or datetime.now(timezone.utc)):
        raise TrialRefused("review-date-passed", "trial.review_after must be a future date")
    row = db.execute(
        "SELECT spawned_by,agent_enabled,history_epoch FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    # A persistent agent may have a spawner; only a temporary worker is refused.
    if row is None or thread_agent_identity(row)[1]:
        raise TrialRefused("child-thread", (
            "a temporary worker keeps no trial lessons: return the finding to "
            "your parent in your plan, and leave this entry as it is"
        ))
    _additive_only(decision)
    epoch = int(entry.get("history_epoch", row["history_epoch"]))
    capture_id = lesson_id(thread_id, epoch, int(entry["entry_id"]))
    hold = _incomplete(db, thread_id, epoch, entry, decision)
    if hold is not None:
        return Admission(capture_id, capture_id, hold)
    if destination.path is None:
        raise TrialRefused("invalid-output", (
            "skill_name must be a new lowercase-hyphenated name for the trial skill"
        ))
    open_trials, retired_trials = _target_trials(place, decision)
    open_rows = tuple(_trial_row(current) for _path, current in open_trials)
    retired_rows = tuple(_trial_row(current) for _path, current in retired_trials)
    retired_match = next(((path, current) for path, current in retired_trials if _same_case(current, decision)), None)
    if retired_match and decision.new_evidence is None:
        raise TrialRefused("retired-match", (
            f"retired trial '{retired_match[1].name}' has the same target, section, and cause; "
            "set new_evidence to target_changed or case_differs to admit a new trial"
        ))
    merge = next(((path, current) for path, current in open_trials if _same_case(current, decision)), None)
    write_path = merge[0] if merge else destination.path
    capture = None
    if place.tray is not None:
        path = lesson_files.capture_path(place.tray, capture_id)
        capture = lesson_files.read_capture(path.read_text(encoding="utf-8")) if path.exists() else None
    identity, text = _render(thread_id, capture_id, entry, decision, write_path, capture)
    _ensure_name_available(db, place, str(decision.skill_name), write_path)
    return Admission(
        identity, capture_id, None, text, write_path, open_rows, retired_rows,
        merge[1].name if merge else None,
    )


def _incomplete(
    db: sqlite3.Connection, thread_id: str, epoch: int, entry: dict[str, Any], decision: Decision,
) -> str | None:
    """Why a hypothesis is not yet complete enough to offer, or None."""
    trial = decision.trial
    assert trial is not None
    if human_cited(entry):
        if not trial.derivation:
            return "missing-derivation"
        if not _literal_resolves(db, thread_id, epoch, entry, trial.literal_ref):
            return "literal-unresolved"
    if not decision.skill_description or not trial.boundary:
        return "no-selective-trigger"
    return None


def _literal_resolves(
    db: sqlite3.Connection, thread_id: str, epoch: int, entry: dict[str, Any],
    ref: dict[str, Any] | None,
) -> bool:
    """Whether ``ref`` names a record of the same person's words that outlives the trial.

    More than a test that something exists: the record must cite a thing a
    person said that the hypothesis also cites, and must not itself be a
    hypothesis. ``{entry_id}`` is another active entry of this thread that is
    not placed as a trial. ``{destination, quote}`` is a line in a file this
    thread is delivered, which is not a lesson file and which one of the
    thread's entries citing that source was placed in. Whether the record's
    text is the person's exact words is not judged here.
    """
    cited = human_refs(entry["body"])
    if not ref or not cited:
        return False
    if "entry_id" in ref:
        if int(ref["entry_id"]) == int(entry["entry_id"]):
            return False
        row = db.execute(
            "SELECT body,placement_state,placement_detail FROM continuity_entries "
            "WHERE thread_id=? AND history_epoch=? AND entry_id=? AND state='active'",
            (thread_id, epoch, int(ref["entry_id"])),
        ).fetchone()
        return row is not None and not _on_trial(row) and bool(cited & _cited_by(row))
    from core.placement_receipt import NotThere, verify
    from infra.config import WORKSPACE

    try:
        destination = verify(db, thread_id, str(ref.get("destination")), str(ref.get("quote")))
    except NotThere:
        return False
    text = (Path(WORKSPACE) / destination).read_text(encoding="utf-8", errors="replace")
    if lesson_files.read_trial(text) is not None:
        return False
    placed = db.execute(
        "SELECT body,placement_detail FROM continuity_entries WHERE thread_id=? "
        "AND placement_state IN ('written','already')",
        (thread_id,),
    ).fetchall()
    return any(
        _placed_at(str(row["placement_detail"] or ""), destination) and cited & _cited_by(row)
        for row in placed
    )


def _placed_at(detail: str, destination: str) -> bool:
    """Whether a written or already mark names ``destination`` (some add a commit)."""
    return detail == destination or detail.startswith(f"{destination} (")


def _cited_by(row: sqlite3.Row) -> set[tuple[str, str]]:
    return human_refs(json.loads(str(row["body"])))


def _on_trial(row: sqlite3.Row) -> bool:
    """Whether an entry is itself queued as a trial or held as a captured hypothesis."""
    state, detail = row["placement_state"], str(row["placement_detail"] or "")
    if state == "stays":
        return detail.startswith("captured:")
    if state != "selected":
        return False
    try:
        answer = json.loads(detail).get("answer") or {}
    except (ValueError, AttributeError):
        return False
    return (answer.get("placement") or {}).get("trial") is not None


def _render(
    thread_id: str, capture_id: str, entry: dict[str, Any], decision: Decision, path: Path,
    capture: lesson_files.CaptureFile | None,
) -> tuple[str, str]:
    """The trial's identity and its file text, against what the path holds now.

    An own trial already there is revised and keeps its identity and evidence.
    An own retired or promoted lesson is captured again, with its origin kept.
    Anything else at the path is someone else's name.
    """
    from core.skill_validation import validate_skill_text

    trial = decision.trial
    assert trial is not None
    from core.lesson_corrections import merge_subjects

    identity, origin, earlier = capture_id, None, ()
    name = str(decision.skill_name)
    subjects = merge_subjects(
        capture.originating_subjects if capture is not None else (), decision.originating_subjects,
    )
    if path.exists():
        current = lesson_files.read_trial(path.read_text(encoding="utf-8", errors="replace"))
        if current is None or current.lesson.owner != thread_id:
            raise TrialRefused("name-taken", (
                f"skill_name {decision.skill_name} is already a skill that is not "
                "your trial lesson; choose a new name"
            ))
        if current.lesson.state in ("watch", "trial"):
            identity, earlier, name = current.lesson.id, current.lesson.source_refs, current.name
            subjects = merge_subjects(current.lesson.originating_subjects, subjects)
        elif current.lesson.id != capture_id:
            origin = {"id": current.lesson.id, "behavioral_sha256": current.behavioral_sha256}
    text = lesson_files.render_trial(
        lesson_id=identity, owner=thread_id, name=name,
        description=str(decision.skill_description), text=decision.text, trial=trial,
        source_refs=lesson_files.merged_refs(earlier, entry["body"].get("source_refs") or []),
        originating_subjects=subjects, origin=origin,
        target_file="new" if decision.target_file == "new" else _target(decision),
        new_target_path=_target(decision) if decision.target_file == "new" else None,
        target_section=decision.target_section,
        cause=decision.cause, case=decision.case,
    )
    errors = validate_skill_text(text).errors
    if errors:
        raise TrialRefused("invalid-output", f"{errors[0]}. Correct it and call again")
    return identity, text


def _ensure_name_available(
    db: sqlite3.Connection, place: ThreadPlace, name: str, path: Path,
) -> None:
    """Refuse a lesson name already held by another resolved skill."""
    from core.skill_resolution import resolve_effective_skills
    from infra.config import WORKSPACE

    skills = resolve_effective_skills(
        thread_id=place.thread_id, project_id=place.project_id, workspace=Path(WORKSPACE), db=db,
    )
    target = path.resolve()
    for row in (*skills.included, *skills.omitted):
        if row.name == name and row.path.resolve() != target:
            raise TrialRefused("name-taken", (
                f"skill_name {name} is already a skill at {row.path}; a trial "
                "never replaces one. Choose a new name"
            ))
    return None
