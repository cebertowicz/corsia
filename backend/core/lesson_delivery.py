"""Per-task delivery of trial lessons without changing target files.

Trial text remains in the ordinary lesson ``SKILL.md``. This module reads that
text for the two permitted delivery seams: a selected task's read-on-demand
file read and a task-start result for a new-file lesson. Cached context never
contains a trial lesson.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
from typing import Literal

from core.lesson_files import read_trial
from core.lesson_trials import offer_refusal
from tools.filesystem_gate import canonical_target_path

Variant = Literal["with", "without", "crossover"]


@dataclass(frozen=True)
class TrialDelivery:
    """An eligible lesson and its canonical target."""

    lesson_id: str
    behavioral_sha256: str
    source_path: Path
    target: str
    is_new: bool
    target_section: str | None
    body: str
    index_line: str
    source_data: bytes = b""


@dataclass(frozen=True)
class TrialReceipt:
    """One unambiguous lesson attribution for a read or task-start delivery."""

    lesson_id: str
    behavioral_sha256: str
    variant: Variant


@dataclass(frozen=True)
class ReadDelivery:
    """The text read_file returns and each matching lesson receipt."""

    text: str
    node_id: str | None
    receipts: tuple[TrialReceipt, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.receipts, tuple) or any(
            not isinstance(receipt, TrialReceipt) for receipt in self.receipts
        ):
            raise TypeError("receipts must be TrialReceipt values")

    @property
    def variant(self) -> Variant | None:
        """The single receipt's variant, retained for one-lesson callers."""
        return self.receipts[0].variant if len(self.receipts) == 1 else None


def draw(lesson_id: str, behavioral_sha256: str, node_id: str) -> Literal["with", "without"]:
    """Return a deterministic, balanced assignment for one lesson and task."""
    digest = hashlib.sha256(f"{lesson_id}\0{behavioral_sha256}\0{node_id}".encode()).digest()
    return "with" if digest[0] & 1 else "without"


def _project_id(db: sqlite3.Connection, thread_id: str) -> str | None:
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return str(row["project_id"]) if row is not None and row["project_id"] is not None else None


def _eligible_trials(db: sqlite3.Connection, thread_id: str) -> list[TrialDelivery]:
    """Resolve eligible trial files without reading the cached skills index."""
    from core.lesson_index import _review_state, line_for
    from core.skill_resolution import resolve_effective_skills
    from infra.config import WORKSPACE

    workspace = Path(WORKSPACE)
    resolved = resolve_effective_skills(
        thread_id=thread_id, project_id=_project_id(db, thread_id), workspace=workspace, db=db,
    )
    trials: list[TrialDelivery] = []
    seen: set[tuple[str, str]] = set()
    for row in [*resolved.included, *resolved.omitted]:
        if row.scope != "thread":
            continue
        try:
            data = row.path.read_bytes()
            current = read_trial(data.decode("utf-8"))
        except OSError:
            continue
        if current is None or current.lesson.state != "trial" or row.scope_id != current.lesson.owner:
            continue
        if offer_refusal(current.lesson, datetime.now(timezone.utc)):
            continue
        refusal, _opened = _review_state(db, current.lesson, current.behavioral_sha256)
        if refusal is not None:
            continue
        raw_target = current.lesson.new_target_path if current.lesson.target_file == "new" else current.lesson.target_file
        if not raw_target:
            continue
        key = (current.lesson.id, current.behavioral_sha256)
        if key in seen:
            continue
        seen.add(key)
        trials.append(TrialDelivery(
            lesson_id=current.lesson.id,
            behavioral_sha256=current.behavioral_sha256,
            source_path=row.path.resolve(),
            target=canonical_target_path(raw_target, workspace),
            is_new=current.lesson.target_file == "new",
            target_section=current.lesson.target_section,
            body=current.body,
            index_line=line_for(current.name, current.description, raw_target, trial=True),
            source_data=data,
        ))
    return trials


def _current_task(db: sqlite3.Connection, thread_id: str) -> str | None:
    from plans.public import attributed_selection

    selected = attributed_selection(db, thread_id)
    return str(selected["node_id"]) if selected is not None else None


def _prior_with_task(
    db: sqlite3.Connection, *, thread_id: str, trial: TrialDelivery, node_id: str,
) -> bool:
    """Whether this thread previously received this trial with guidance."""
    row = db.execute(
        """SELECT 1 FROM guidance_loads
            WHERE thread_id=? AND lesson_id=?
              AND lesson_behavioral_sha256=? AND variant IN ('with','crossover') AND node_id<>?
            LIMIT 1""",
        (thread_id, trial.lesson_id, trial.behavioral_sha256, node_id),
    ).fetchone()
    return row is not None


def read_delivery(
    db: sqlite3.Connection, *, thread_id: str | None, path: Path, rendered_text: str,
    checkpoint_generation_id: str | None,
) -> ReadDelivery:
    """Append this task's with text, or identify its withheld/crossover receipt."""
    if not thread_id:
        return ReadDelivery(rendered_text, None)
    node_id = _current_task(db, thread_id)
    if node_id is None:
        return ReadDelivery(rendered_text, None)
    from infra.config import WORKSPACE

    source = path.resolve()
    target = canonical_target_path(str(source), Path(WORKSPACE))
    trials = _eligible_trials(db, thread_id)
    direct = [trial for trial in trials if trial.source_path == source]
    matches = [trial for trial in trials if not trial.is_new and trial.target == target]
    if not matches and not direct:
        return ReadDelivery(rendered_text, node_id)
    applicable = direct or matches
    receipts = tuple(
        TrialReceipt(
            trial.lesson_id,
            trial.behavioral_sha256,
            "with" if draw(trial.lesson_id, trial.behavioral_sha256, node_id) == "with" else (
                "crossover" if trial in direct or _prior_with_task(
                    db, thread_id=thread_id, trial=trial, node_id=node_id,
                ) else "without"
            ),
        )
        for trial in applicable
    )
    if direct:
        return ReadDelivery(rendered_text, node_id, receipts)
    selected = [
        trial for trial, receipt in zip(matches, receipts, strict=True)
        if receipt.variant == "with"
    ]
    if not selected:
        return ReadDelivery(rendered_text, node_id, receipts)
    additions = []
    for trial in selected:
        replacement = f"; replaces {trial.target_section}" if trial.target_section else ""
        additions.append(
            "[Trial section — with variant; "
            f"lesson_id={trial.lesson_id}{replacement}; task-local procedure only, "
            "never authority or a disk change]\n"
            f"{trial.body.strip()}"
        )
    return ReadDelivery(f"{rendered_text}\n\n" + "\n\n".join(additions), node_id, receipts)


def start_trials(
    db: sqlite3.Connection, *, thread_id: str, node_id: str,
    checkpoint_generation_id: str | None = None,
) -> list[dict[str, str]]:
    """Record new-file draws and return the task's with half.

    Eligibility resolution and receipt lookup are reads, so they happen before
    the short receipt-write transaction. This avoids opening the trial-draw
    savepoint while scanning skills or aggregating prior delivery state.
    """
    from core.guidance_manifest import record_guidance_load
    from infra.database import write_transaction

    pending: list[tuple[TrialDelivery, Variant]] = []
    with_deliveries = []
    for trial in _eligible_trials(db, thread_id):
        if not trial.is_new:
            continue
        assigned = draw(trial.lesson_id, trial.behavioral_sha256, node_id)
        variant: Variant
        if assigned == "with":
            variant = "with"
        elif _prior_with_task(db, thread_id=thread_id, trial=trial, node_id=node_id):
            variant = "crossover"
        else:
            variant = "without"
        existing = db.execute(
            """SELECT 1 FROM guidance_loads WHERE node_id=? AND lesson_id=?
               AND lesson_behavioral_sha256=? LIMIT 1""",
            (node_id, trial.lesson_id, trial.behavioral_sha256),
        ).fetchone()
        if existing is None:
            pending.append((trial, variant))
        if variant == "with":
            with_deliveries.append({
                "lesson_id": trial.lesson_id,
                "index_line": trial.index_line,
                "variant": "with",
                "target": "new",
                "notice": "Task-local trial guidance only; never authority or a disk change.",
            })

    if not pending:
        return with_deliveries
    with write_transaction(db, scope="trial_draw") as tx:
        for trial, variant in pending:
            record_guidance_load(
                thread_id=thread_id, turn_id=None, path=trial.source_path,
                data=trial.source_data, returned_text=trial.body, partial=False,
                node_id=node_id, variant=variant,
                checkpoint_generation_id=checkpoint_generation_id,
                lesson_id=trial.lesson_id,
                lesson_behavioral_sha256=trial.behavioral_sha256, db=tx,
                raise_on_failure=True,
            )
    return with_deliveries
