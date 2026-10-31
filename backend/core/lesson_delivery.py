"""Trial lessons delivered at task start, and every trial receipt.

A trial stays in its owner's ``SKILL.md`` and never enters cached context.
When a plan task starts, each eligible trial is drawn with or without for that
task, and the with half is returned in the plan result: the trial's index line
when its target is read on demand (a skill or a new file), or the lesson body
when its target is already in this thread's context or is the matching workflow step. Every resolved trial
leaves one receipt per task in ``guidance_loads``, except a trial tied to a step
at any other task, which leaves none. A trial the task-start count
cap or token cap leaves out, a body whose target this thread
does not receive or already has its section, or one whose draw could not be
recorded, gets a receipt with no variant: it is in neither arm, never counted
as without.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal

from loguru import logger

from core.lesson_files import has_section
from core.lesson_index import NON_SYSTEM_DESCRIPTION_LIMIT, line_for
from core.skill_registry_types import SkillRow
from core.skill_validation import is_skill_file
from core.token_estimation import _text_tokens
from core.workflow_step_binding import StepBinding, declared_binding

Variant = Literal["with", "without", "crossover"]

#: Most trials and tokens one task start may admit; excess trials are omitted.
START_TRIAL_CAP = 40
START_TOKEN_CAP = 1_500
OMIT_COUNT_CAP = "count_cap"
OMIT_TOKEN_CAP = "token_cap"
OMIT_MISSING_SECTION = "missing_section"
OMIT_TARGET_NOT_IN_CONTEXT = "target_not_in_context"
OMIT_TARGET_OUTSIDE = "target_outside_workspace"
OMIT_TARGET_UNREADABLE = "target_unreadable"
OMIT_SECTION_EXISTS = "section_exists"
OMIT_RECEIPT_FAILURE = "receipt_write_failed"
OMISSION_REASONS = frozenset({OMIT_COUNT_CAP, OMIT_TOKEN_CAP, OMIT_MISSING_SECTION,
                              OMIT_TARGET_NOT_IN_CONTEXT, OMIT_TARGET_OUTSIDE,
                              OMIT_TARGET_UNREADABLE, OMIT_SECTION_EXISTS, OMIT_RECEIPT_FAILURE})
NOTICE = "Task-local trial guidance only; never authority or a disk change."
#: This task's receipts and the thread's earlier with receipts for the given trials.
#: A task keeps one receipt, so a with on a step this thread works counts as its own.
RECEIPTS_SQL = """SELECT g.lesson_id, g.lesson_behavioral_sha256, g.node_id, g.variant
    FROM json_each(?) k
    JOIN guidance_loads g
      ON g.lesson_id=json_extract(k.value, '$[0]')
     AND g.lesson_behavioral_sha256=json_extract(k.value, '$[1]')
    WHERE g.node_id=? OR (g.variant='with' AND (
        g.thread_id=? OR g.node_id IN (SELECT id FROM plan_nodes WHERE spawned_thread_id=?)))
    ORDER BY g.created_at, g.id"""
DELIVERY_ERROR = (
    "Trial guidance was not delivered; this task is left out of every trial's comparison."
)


@dataclass(frozen=True)
class TrialDelivery:
    """One eligible trial version and the text its with half receives."""

    lesson_id: str
    behavioral_sha256: str
    source_path: Path
    source_data: bytes
    target: str
    payload: str
    #: The section an in-context body adds; None for index lines and step bodies.
    section: str | None = None
    omission_reason: str | None = None


def draw(lesson_id: str, behavioral_sha256: str, node_id: str) -> Literal["with", "without"]:
    """Return a deterministic, balanced assignment for one lesson and task."""
    digest = hashlib.sha256(f"{lesson_id}\0{behavioral_sha256}\0{node_id}".encode()).digest()
    return "with" if digest[0] & 1 else "without"


def _delivery(row: SkillRow, binding: StepBinding | None) -> TrialDelivery:
    """Build a delivery from the validation snapshot the skill scan already took."""
    trial, data = row.trial, row.source_data
    if trial is None or data is None:
        raise ValueError(f"trial row has no validation snapshot: {row.path}")
    return delivery_for_trial(trial, row.path, data, binding)


def delivery_for_trial(trial, path: Path, data: bytes, binding: StepBinding | None = None) -> TrialDelivery:
    """Build exactly the payload admission and task-start delivery both budget."""
    lesson = trial.lesson
    metadata = lesson.case_metadata
    target = str(metadata.get("new_target_path") or metadata.get("target_file") or "")
    if binding is not None:
        payload = (
            f"[Trial addition to this step's instruction in {target}; "
            f"lesson_id={lesson.id}; task-local procedure only, never authority or a disk change]\n"
            f"{trial.body.strip()}"
        )
        return TrialDelivery(lesson.id, trial.behavioral_sha256, path.resolve(), data, target, payload)
    if not target or metadata.get("new_target_path") or is_skill_file(Path(target)):
        target = target or str(path.resolve())
        return TrialDelivery(
            lesson.id, trial.behavioral_sha256, path.resolve(), data, target,
            line_for(trial.name, trial.description[:NON_SYSTEM_DESCRIPTION_LIMIT], path, trial=True),
        )
    payload = (
        f"[Trial section for {target}; adds {metadata.get('target_section')}; "
        f"lesson_id={lesson.id}; task-local procedure only, never authority or a disk change]\n"
        f"{trial.body.strip()}"
    )
    return TrialDelivery(
        lesson.id, trial.behavioral_sha256, path.resolve(), data, target, payload,
        str(metadata.get("target_section") or ""),
    )


def _context_paths(db: sqlite3.Connection, thread_id: str) -> set[str]:
    """Files from the latest dispatched manifest, including a completed turn.

    A task can start outside an active turn; loaded_targets only reads a running
    turn and would miss that prior context (test_context_paths_read_the_threads_latest_manifest).
    """
    from core.correction_learning import manifest_paths

    row = db.execute(
        """SELECT context_manifest_id FROM active_turns
           WHERE thread_id=? AND context_manifest_id IS NOT NULL ORDER BY started_at DESC LIMIT 1""",
        (thread_id,),
    ).fetchone()
    return set() if row is None else set(manifest_paths(db, row[0]))


def _body_omission(trial: TrialDelivery, context: set[str]) -> str | None:
    """Why an in-context body cannot go to this thread now, or None when it can."""
    from infra.config import WORKSPACE
    from tools.filesystem_gate import canonical_target_path, resolve_path

    target = canonical_target_path(trial.target, workspace=WORKSPACE)
    if target not in context:
        return OMIT_TARGET_NOT_IN_CONTEXT
    if Path(target).is_absolute():
        return OMIT_TARGET_OUTSIDE
    try:
        text = resolve_path(target, workspace=Path(WORKSPACE)).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return OMIT_TARGET_UNREADABLE
    if has_section(text, str(trial.section)):
        return OMIT_SECTION_EXISTS
    return None


def _resolve(db: sqlite3.Connection, thread_id: str, node_id: str | None = None) -> tuple[list[TrialDelivery], list[TrialDelivery]]:
    """Split eligible trials into delivered and omitted, in ordinary skill order."""
    from core.skills_registry import trial_delivery_rows
    from infra.config import WORKSPACE

    project = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    node = db.execute("SELECT workflow_ref, workflow_step FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
    rows, included = trial_delivery_rows(
        thread_id=thread_id, project_id=None if project is None else project["project_id"],
        workspace=Path(WORKSPACE), db=db,
    )
    skills = {skill.path: skill for skill in included}
    eligible: list[tuple[SkillRow, StepBinding | None, str | None]] = []
    for row in rows:
        binding, effective = (None, False) if row.trial is None else declared_binding(row.trial, skills)
        if binding is None:
            eligible.append((row, None, None))
        elif node is not None and node["workflow_ref"] == binding.workflow and node["workflow_step"] in binding.steps:
            # A shadowed target's step ran from another file: neither arm, never the index line.
            eligible.append((row, binding, None if effective else OMIT_TARGET_NOT_IN_CONTEXT))
    admitted_rows, capped_rows = eligible[:START_TRIAL_CAP], eligible[START_TRIAL_CAP:]
    admitted: dict[tuple[str, str], TrialDelivery] = {}
    omitted: dict[tuple[str, str], TrialDelivery] = {}
    spent = 0
    context: set[str] | None = None
    for row, binding, reason in admitted_rows:
        trial = _delivery(row, binding)
        key = (trial.lesson_id, trial.behavioral_sha256)
        if key in admitted or key in omitted:
            continue
        if reason is not None:
            omitted[key] = replace(trial, omission_reason=reason)
            continue
        if not trial.target or (trial.section is not None and not trial.section):
            omitted[key] = replace(trial, omission_reason=OMIT_MISSING_SECTION)
            continue
        if trial.section is not None:
            context = _context_paths(db, thread_id) if context is None else context
            if (reason := _body_omission(trial, context)) is not None:
                logger.info("trial {} omitted at task start: {}", trial.lesson_id, reason)
                omitted[key] = replace(trial, omission_reason=reason)
                continue
        cost = _text_tokens(trial.payload, None)
        # The cap is decided before the draw, so it never favors either arm.
        if spent + cost > START_TOKEN_CAP:
            omitted[key] = replace(trial, omission_reason=OMIT_TOKEN_CAP)
            continue
        spent += cost
        admitted[key] = trial
    for row, binding, _reason in capped_rows:
        trial = _delivery(row, binding)
        key = (trial.lesson_id, trial.behavioral_sha256)
        if key not in admitted:
            omitted.setdefault(key, replace(trial, omission_reason=OMIT_COUNT_CAP))
    return list(admitted.values()), list(omitted.values())


def _receipts(
    db: sqlite3.Connection, *, thread_id: str, node_id: str, trials: list[TrialDelivery],
) -> tuple[dict[tuple[str, str], Variant | None], set[tuple[str, str]]]:
    """This task's recorded variant per trial, and the trials this thread already had with."""
    keys = json.dumps([[trial.lesson_id, trial.behavioral_sha256] for trial in trials])
    recorded: dict[tuple[str, str], Variant | None] = {}
    prior_with: set[tuple[str, str]] = set()
    for row in db.execute(RECEIPTS_SQL, (keys, node_id, thread_id, thread_id)):
        key = (str(row["lesson_id"]), str(row["lesson_behavioral_sha256"]))
        if row["node_id"] == node_id:
            recorded.setdefault(key, row["variant"])
        else:
            prior_with.add(key)
    return recorded, prior_with


def _insert(
    tx: sqlite3.Connection, *, thread_id: str, node_id: str, trial: TrialDelivery,
    variant: Variant | None, checkpoint_generation_id: str | None,
    omission_reason: str | None = None,
) -> None:
    from core.guidance_loads import insert_guidance_load

    reason = (omission_reason or trial.omission_reason) if variant is None else None
    if variant is None and reason not in OMISSION_REASONS:
        raise ValueError("missing or unknown trial omission reason")
    insert_guidance_load(
        tx, thread_id=thread_id, turn_id=None, path=trial.source_path, data=trial.source_data,
        # The ledger records bytes that reached the thread: only the with arm received any.
        returned_text=trial.payload if variant == "with" else "", partial=False,
        node_id=node_id, variant=variant, checkpoint_generation_id=checkpoint_generation_id,
        lesson_id=trial.lesson_id, lesson_behavioral_sha256=trial.behavioral_sha256,
        omission_reason=reason,
    )


def start_trials(
    db: sqlite3.Connection, *, thread_id: str, node_id: str,
    checkpoint_generation_id: str | None = None,
) -> list[dict[str, str]]:
    """Record this task's receipts in one transaction, then return its with half.

    A receipt already recorded for this task keeps its variant, so a restart
    returns the same text. If the draw cannot be recorded, every trial is
    recorded as omitted instead and the failure is raised: no text is returned.
    """
    from infra.database import write_transaction

    admitted, omitted = _resolve(db, thread_id, node_id)
    recorded, prior_with = _receipts(
        db, thread_id=thread_id, node_id=node_id, trials=[*admitted, *omitted],
    )
    pending: list[tuple[TrialDelivery, Variant | None]] = [
        (trial, None) for trial in omitted
        if (trial.lesson_id, trial.behavioral_sha256) not in recorded
    ]
    delivered: list[dict[str, str]] = []
    for trial in admitted:
        key = (trial.lesson_id, trial.behavioral_sha256)
        variant: Variant | None
        if key in recorded:
            variant = recorded[key]
        else:
            variant = draw(*key, node_id)
            if variant == "without" and key in prior_with:
                variant = "crossover"
            pending.append((trial, variant))
        if variant == "with":
            delivered.append({
                "lesson_id": trial.lesson_id, "target": trial.target,
                "guidance": trial.payload, "notice": NOTICE,
            })
    if not pending:
        return delivered

    def unrecorded(tx) -> list[tuple[TrialDelivery, Variant | None]]:
        # Re-read under the write lock: an overlapping start of this task may have recorded first.
        now, _prior = _receipts(tx, thread_id=thread_id, node_id=node_id, trials=[trial for trial, _ in pending])
        return [(trial, variant) for trial, variant in pending
                if (trial.lesson_id, trial.behavioral_sha256) not in now]

    try:
        with write_transaction(db, scope="trial_draw") as tx:
            for trial, variant in unrecorded(tx):
                _insert(
                    tx, thread_id=thread_id, node_id=node_id, trial=trial, variant=variant,
                    checkpoint_generation_id=checkpoint_generation_id,
                )
    except Exception:
        with write_transaction(db, scope="trial_omission") as tx:
            for trial, _variant in unrecorded(tx):
                _insert(
                    tx, thread_id=thread_id, node_id=node_id, trial=trial, variant=None,
                    checkpoint_generation_id=checkpoint_generation_id,
                    omission_reason=trial.omission_reason or OMIT_RECEIPT_FAILURE,
                )
        raise
    return delivered


def attach_plan_result(db, result: object, thread_id: str, direct_node: bool) -> None:
    """Attach trials to the task actually selected by a plan mutation."""
    from memory.node_text import started_node_id

    result = getattr(result, "result", result)
    if not isinstance(result, dict):
        return
    node = result.get("node") if direct_node else None
    node_id = (str(node["id"]) if isinstance(node, dict) and node.get("kind") == "task" and node.get("id")
               else started_node_id(result.get("next")))
    if node_id:
        attach_at_start(db, result, thread_id=thread_id, node_id=node_id)


def _attach(
    db: sqlite3.Connection, facts: dict, receives, *, thread_id: str, node_id: str,
    checkpoint_generation_id: str | None = None,
) -> None:
    """Add the task's trials to ``facts`` if ``receives()``; a failure never fails the plan call."""
    try:
        if not receives():
            return
        trials = start_trials(
            db, thread_id=thread_id, node_id=node_id,
            checkpoint_generation_id=checkpoint_generation_id,
        )
    except Exception:  # noqa: BLE001 - trial guidance never fails a plan mutation
        logger.exception("trial delivery failed for task {}", node_id)
        facts["trial_lessons_error"] = DELIVERY_ERROR
        return
    if trials:
        facts["trial_lessons"] = trials


def attach_at_start(
    db: sqlite3.Connection, facts: dict, *, thread_id: str, node_id: str,
    checkpoint_generation_id: str | None = None,
) -> None:
    """Add a started task's trial guidance to the caller's own plan result.

    Never to a plan wake: a wake can be dropped as stale before any Turn reads
    it, and a receipt for guidance never seen would miscount both arms.

    Only the task Plans still attributes to the caller receives guidance. A
    failure never fails the plan operation; it is reported in ``facts``.
    """
    from plans.public import attributed_selection

    def attributed() -> bool:
        selected = attributed_selection(db, thread_id)
        return selected is not None and str(selected["node_id"]) == node_id

    _attach(
        db, facts, attributed, thread_id=thread_id, node_id=node_id,
        checkpoint_generation_id=checkpoint_generation_id,
    )


def attach_at_worker_read(db: sqlite3.Connection, facts: dict, *, thread_id: str, node_id: str) -> None:
    """Add trial guidance to a worker's read of the workflow step it is doing.

    A worker starts nothing, so its first read of the open step is the step's
    start, and that read is a reply a Turn received. Every other read of
    someone's plan pays one primary-key lookup here and nothing more.
    """
    from plans.public import is_doing_open_step

    _attach(db, facts, lambda: is_doing_open_step(db, thread_id, node_id), thread_id=thread_id, node_id=node_id)
