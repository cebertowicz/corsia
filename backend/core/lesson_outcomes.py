"""What the placement worker does with one trial answer.

A complete hypothesis is activated in three steps: the trial skill is written
without a review reference, so no index offers it; its owner's review is
ensured in one database write; only then is the reference written, and the
next installed index offers the trial. A held hypothesis is captured: a small
file in the owner's tray and a review, never offered, with its continuity
entry left in place so the owner can complete it.

Every step finds what an earlier attempt left, so a retry after any failure
repeats nothing. Until the last step the trial stays out of the index.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from core import lesson_files
from core import placement_outcomes as outcomes
from core.lesson_admission import Admission, TrialRefused, admit
from core.lesson_trials import capture_hash
from core.placement_decide import Decision, Destination, ThreadPlace

TRIAL_ACCEPTANCE = (
    "The trial lesson is retired, promoted or extended, with the evidence for that choice."
)
WATCH_ACCEPTANCE = "The watch is promoted to a trial or retired, with the recurrence evidence for that choice."
TRIAL_STEPS = (
    "After its review date this trial lesson is left out the next time a skills "
    "list is rebuilt; a conversation that already lists it keeps it. Read the skill "
    "file linked to this task, decide from what happened while it was listed, "
    "and finish this task with that evidence.\n"
    "- Retire: set corsia.lesson.state to retired in the file.\n"
    "- Promote: write the rule into its permanent home, then set state to "
    "promoted and destination {path, version}.\n"
    "- Extend: set a later corsia.lesson.review_after and schedule this task "
    "for that date instead of finishing it.\n"
    "- Revise: record the changed rule as a continuity entry and place it with "
    "verdict trial under the same skill_name; that closes this task."
)
WATCH_STEPS = (
    "This watch is not delivered as guidance. Review its linked correction anchors: "
    "promote this same lesson record to trial on recurrence, or retire it with evidence."
)

CAPTURE_ACCEPTANCE = (
    "The captured hypothesis is completed into a trial lesson or dropped, with the reason."
)
CAPTURE_STEPS = (
    "This captured hypothesis is not offered as guidance; the file linked to "
    "this task says why it was held. Once resolved (missing fields added or the "
    "conflicting trial finished), place its continuity entry with verdict trial "
    "to close this task, or drop the entry with the reason and finish this task."
)


def place_trial(
    entry: dict[str, Any], place: ThreadPlace, decision: Decision, destination: Destination,
) -> None:
    """End one trial answer in exactly one logged outcome."""
    from infra.database import get_db

    db = get_db()
    try:
        admission: Admission | TrialRefused = admit(
            db, entry["thread_id"], entry, place, decision, destination,
        )
    except TrialRefused as refused:
        admission = refused
    finally:
        db.close()
    if isinstance(admission, TrialRefused):
        outcomes.settle(entry, "unplaced", admission.code)
    elif admission.hold is None:
        _activate(entry, place, decision, destination, admission)
    else:
        _capture(entry, place, decision, admission)


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _save(
    entry: dict[str, Any], place: ThreadPlace, decision: Decision, relative: str,
    writes: dict[Path, str], expected: dict[Path, str | None], verb: str,
) -> bool:
    """Commit one step's files; on failure settle or defer the entry and say no."""
    from core.placement_proposal import marker
    from infra.config import WORKSPACE
    from infra.guidance_git import save_guidance
    from infra.guidance_targets import GuidanceTarget
    from infra.skill_save_validation import REFUSED

    trailer = marker(entry["thread_id"], entry["entry_id"], entry["revision"])
    result = save_guidance(
        {path: text.encode() for path, text in writes.items()},
        workspace=Path(WORKSPACE), target=GuidanceTarget("placement"),
        expected=expected, atomic=True, author=outcomes._author(place),
        message=outcomes._message(entry, decision, relative, trailer, verb=verb),
        lesson_gate=False,  # the step's own retire and promote path
    )
    if result.outcome in ("written", "unchanged"):
        return True
    if result.outcome in ("deferred", "stale"):
        outcomes.defer(entry, result.reason, destination=relative)
    elif result.outcome == REFUSED:
        outcomes.refuse(entry, relative, result.errors)
    else:
        reason = "history-off" if result.outcome == "history-off" else result.reason
        outcomes.settle(entry, "unplaced", reason or result.outcome, destination=relative)
    return False


def _review(
    entry: dict[str, Any], path: Path, relative: str, ensure: Callable[..., tuple[str, bool]],
) -> str | None:
    """Register the file and ensure its review in one write; None when settled instead."""
    from infra.database import get_db, write_transaction
    from plans import public as plans
    from registry.service import register_write_strict

    db = get_db()
    try:
        with write_transaction(db, scope="lesson_review") as connection:
            artifact = register_write_strict(entry["thread_id"], str(path), db=connection)
            node_id, _created = ensure(connection, entry["thread_id"], artifact_id=artifact["id"])
            return node_id
    except plans.LessonOwnerError as missing:
        logger.warning("lesson owner cannot hold a review thread={}: {}", entry["thread_id"], missing)
        outcomes.settle(entry, "unplaced", "owner-missing", destination=relative)
        return None
    finally:
        db.close()


def _activate(
    entry: dict[str, Any], place: ThreadPlace, decision: Decision, destination: Destination,
    admission: Admission,
) -> None:
    from functools import partial

    from infra.guidance_git import digest
    from plans import public as plans

    path, base = admission.destination_path or destination.path, admission.text
    assert path is not None and base is not None and place.tray is not None
    trial = lesson_files.read_trial(base)
    assert trial is not None
    relative = outcomes._relative(path)
    current = _read(path)
    held = lesson_files.read_trial(current) if current else None
    # An unchanged version keeps the review it already has, so it stays offered.
    kept = (held.lesson.review_node_id if held and held.lesson.state == trial.lesson.state
            and held.behavioral_sha256 == trial.behavioral_sha256 else None)
    draft = lesson_files.with_review_ref(base, kept) if kept else base
    if current != draft and not _save(
        entry, place, decision, relative, {path: draft}, {path: digest(current)}, "trial written",
    ):
        return
    is_watch = trial.lesson.state == "watch"
    node_id = _review(entry, path, relative, partial(
        plans.ensure_lesson_review, lesson_id=trial.lesson.id, state=trial.lesson.state,
        behavioral_sha256=trial.behavioral_sha256,
        title=f"Review {'watch' if is_watch else 'trial'} lesson: {trial.name}",
        acceptance=WATCH_ACCEPTANCE if is_watch else TRIAL_ACCEPTANCE,
        review_after=trial.lesson.review_after,
        description=((WATCH_STEPS if is_watch else TRIAL_STEPS) + "\nRead recorded evidence for this exact version with "
                     f"lesson_report(lesson_id={json.dumps(trial.lesson.id)}, "
                     f"version={json.dumps(trial.behavioral_sha256)}). "
                     "Offers and reads show exposure, not application or benefit; evidence excludes the current week."),
        capture_id=admission.capture_id,
    ))
    if node_id is None:
        return
    final = lesson_files.with_review_ref(base, node_id)
    writes, expected = ({path: final}, {path: digest(draft)}) if final != draft else ({}, {})
    capture = lesson_files.capture_path(place.tray, admission.capture_id)
    captured = _read(capture)
    superseded = lesson_files.superseded_capture(captured, trial.lesson.id) if captured else None
    if superseded is not None and superseded != captured:
        writes[capture], expected[capture] = superseded, digest(captured)
    if writes and not _save(entry, place, decision, relative, writes, expected, "trial offered"):
        return
    outcomes.settle(
        entry, "written", f"{trial.lesson.state}-activated", mark="written",
        detail=f"{relative} (review {node_id})", retire=f"placed as a {trial.lesson.state} lesson at {relative}",
        source_ref=f"file:{relative}", destination=relative,
    )


def _capture(
    entry: dict[str, Any], place: ThreadPlace, decision: Decision, admission: Admission,
) -> None:
    from functools import partial

    from infra.guidance_git import digest
    from plans import public as plans

    from core.lesson_corrections import merge_subjects

    trial, reason = decision.trial, admission.hold
    assert trial is not None and reason is not None and place.tray is not None
    path = lesson_files.capture_path(place.tray, admission.capture_id)
    relative = outcomes._relative(path)
    version = capture_hash(decision.text, trial)
    current = _read(path)
    earlier = lesson_files.read_capture(current) if current else None
    # Evidence accumulates only while the hypothesis itself is unchanged.
    kept = earlier.source_refs if earlier is not None and earlier.hash == version else ()
    subjects = earlier.originating_subjects if earlier is not None and earlier.hash == version else ()
    text = lesson_files.render_capture(
        capture_id=admission.capture_id, capture_hash=version, owner=entry["thread_id"],
        reason=reason, name=decision.skill_name, description=decision.skill_description,
        text=decision.text, trial=trial,
        source_refs=lesson_files.merged_refs(kept, entry["body"].get("source_refs") or []),
        originating_subjects=merge_subjects(subjects, decision.originating_subjects),
        case_metadata=admission.case_metadata,
    )
    if current != text and not _save(
        entry, place, decision, relative, {path: text}, {path: digest(current)},
        "hypothesis captured",
    ):
        return
    shown = decision.skill_name or " ".join(decision.text.split()[:6])
    node_id = _review(entry, path, relative, partial(
        plans.ensure_capture_review, capture_id=admission.capture_id, capture_hash=version,
        title=f"Review captured hypothesis: {shown}", acceptance=CAPTURE_ACCEPTANCE,
        review_after=trial.review_after, description=CAPTURE_STEPS,
    ))
    if node_id is None:
        return
    # The entry stays active and marked, so its owner can complete it later.
    outcomes.settle(
        entry, "captured", reason, mark="stays", detail=f"captured:{reason}:{relative}",
        destination=relative,
    )
