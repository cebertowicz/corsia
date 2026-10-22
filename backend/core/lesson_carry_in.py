"""Durable carry-in facts and their after-save drain for lesson guidance."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from core.lesson_carry_in_contract import (
    ACTIONS_FIELDS, ACTION_FIELDS, SAMPLE_COUNT, Action, CarryInRun, LocalTraceAdapter, TraceAdapter, TraceScore, action_matches,
    canonical_calls, rotated_cases, run_three, scenario_for_case, score_trace, validate_actions,
)
from core.lesson_carry_in_history import (
    case_sha as _case_sha,
    recorded_target_blob as _recorded_target_blob,
    target_snapshot as _target_snapshot,
)
from core.lesson_carry_in_result_queries import baseline_result, prior_case_status, result_event

PENDING_EVENT = "learning_loop.carry_in_pending"
RESULT_EVENT = "learning_loop.carry_in_result"
RETIREMENT_EVENT = "learning_loop.carry_in_retirement"
SOURCE_SKIPPED_EVENT = "learning_loop.carry_in_source_skipped"


@dataclass(frozen=True)
class CarryInFact:
    """A queryable check identity; target text never enters outcome facts."""

    lesson_id: str
    owner_thread_id: str
    target_commit: str
    target_path: str
    case_ref: str
    role: str = "edit"

    @property
    def subject_key(self) -> str:
        suffix = "" if self.role == "edit" else f":{self.role}"
        return f"carry-in:{self.lesson_id}:{self.target_commit}{suffix}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_id(db, thread_id: str) -> str | None:
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def target_scope_owner(db, workspace: Path, target_path: str) -> str | None:
    """Resolve the target scope's correction owner without creating a second registry."""
    from core.workspace_layout import thread_tray_owners
    from infra.conductor import get_project_conductor
    from infra.corsia_agent import get_corsia_agent

    target = workspace / target_path
    relative = target.resolve(strict=False).relative_to(workspace.resolve(strict=False))
    if relative.parts and relative.parts[0] == ".corsia":
        raise ValueError("System projections cannot be carry-in targets")
    owners = thread_tray_owners(target)
    if len(owners) == 1:
        return owners[0]
    if len(owners) > 1:
        return None
    if len(relative.parts) >= 2 and relative.parts[0] == "projects":
        return get_project_conductor(relative.parts[1], db=db) or get_corsia_agent(db=db)
    return get_corsia_agent(db=db)


def _record(db, *, event_name: str, fact: CarryInFact, dedupe_suffix: str, attributes: dict[str, Any]) -> bool:
    """Use the one outcome-event writer and its bounded attribute allowlist."""
    from infra.outcome_event_attributes import count_attr, enum_attr, id_attr, text_attr
    from infra.outcome_events import record_outcome_event

    specs = {
        "lesson_id": id_attr(), "owner_thread_id": id_attr(), "target_commit": id_attr(),
        "target_path": id_attr(), "case_ref": id_attr(), "case_sha": id_attr(),
        "case_status": enum_attr("proven", "unproven"),
        "verdict": enum_attr("pass", "fail"), "owner_resolution": enum_attr("resolved", "owner_unresolved"),
        "sample_count": count_attr(), "passed_samples": count_attr(), "target_hash": id_attr(),
        "model_selector": id_attr(), "model_identity": text_attr(500), "blob_sha": id_attr(),
        "commit": id_attr(), "role": enum_attr("edit", "rebaseline"),
        "target_fixture_sha": id_attr(), "delivered_blob_sha": id_attr(),
        "delivered_neighbour_shas": text_attr(4000), "reason": text_attr(500),
    }
    result = record_outcome_event(
        db, dedupe_key=f"{fact.subject_key}:{dedupe_suffix}", event_name=event_name,
        source="corsia", provenance="instrumented", ts=_now(), subject_key=fact.subject_key,
        project_id=_project_id(db, fact.owner_thread_id), agent_thread_id=None,
        attributes={"lesson_id": fact.lesson_id, "owner_thread_id": fact.owner_thread_id,
                    "target_commit": fact.target_commit, "target_path": fact.target_path,
                    "case_ref": fact.case_ref, "role": fact.role, **attributes}, declared_specs=specs,
    )
    return result.inserted


def record_pending(
    db, fact: CarryInFact, *, reason: str | None = None, attempt: int | None = None,
) -> bool:
    """Record a local-model-unavailable carry-in check without adding a queue."""
    suffix = f"pending:attempt:{attempt}" if attempt is not None else "pending"
    if reason is not None and attempt is None:
        suffix = f"pending:{reason}"
    return _record(db, event_name=PENDING_EVENT, fact=fact, dedupe_suffix=suffix, attributes={} if reason is None else {"reason": reason})


def record_source_skipped(
    db, *, lesson_id: str, owner_thread_id: str, target_commit: str, target_path: str,
    reason: str = "outside_workspace",
) -> bool:
    """Count an excluded case without retaining its source path or target text."""
    fact = CarryInFact(lesson_id, owner_thread_id, target_commit, target_path, "skipped-source")
    return _record(
        db, event_name=SOURCE_SKIPPED_EVENT, fact=fact, dedupe_suffix=f"source-{reason}",
        attributes={"reason": reason},
    )


def _is_thread_skill_target(target_path: str) -> bool:
    parts = Path(target_path).parts
    return len(parts) >= 4 and parts[0] == ".threads" and parts[2] == "skills"


def record_result(
    db, fact: CarryInFact, run: CarryInRun, *, owner_unresolved: bool = False,
    case_sha: str | None = None, case_status: str | None = None,
) -> bool:
    """Persist a settled three-sample result; callers keep pending results pending."""
    if run.state != "settled" or run.verdict is None or len(run.scores) != SAMPLE_COUNT:
        raise ValueError("only a settled three-sample carry-in run has a result")
    passed = sum(score.verdict == "pass" for score in run.scores)
    return _record(
        db, event_name=RESULT_EVENT, fact=fact, dedupe_suffix="result",
        attributes={
            "verdict": run.verdict, "owner_resolution": "owner_unresolved" if owner_unresolved else "resolved",
            "sample_count": SAMPLE_COUNT, "passed_samples": passed, "target_hash": run.target_hash,
            "model_selector": run.selector, "model_identity": _model_identity(run),
            "blob_sha": run.delivered_blob_sha or run.target_hash, "case_sha": case_sha,
            "case_status": case_status,
            "commit": fact.target_commit, "role": fact.role, "target_fixture_sha": run.fixture_sha,
            "delivered_blob_sha": run.delivered_blob_sha,
            "delivered_neighbour_shas": json.dumps(run.neighbour_shas),
        },
    )


def _model_identity(run: CarryInRun) -> str | None:
    if run.selector is None or run.identity is None:
        return None
    return json.dumps({"resolved_identity": run.identity, "selector": run.selector}, sort_keys=True)



from core.lesson_carry_in_coverage import coverage_counts


def record_retirement(db, fact: CarryInFact, reason: str) -> bool:
    """Record the owner-supplied, queryable retirement reason without case source text."""
    if not reason.strip():
        raise ValueError("a carry-in retirement needs a stated reason")
    return _record(
        db, event_name=RETIREMENT_EVENT, fact=fact, dedupe_suffix="retirement",
        attributes={"reason": reason.strip()},
    )


def is_pending(db, fact: CarryInFact) -> bool:
    """A pending fact has no durable result or retirement fact for the same target edit."""
    row = db.execute(
        """SELECT 1 FROM outcome_events pending
           WHERE pending.event_name=? AND pending.subject_key=? AND pending.retracted_at IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM outcome_events terminal
               WHERE terminal.subject_key=pending.subject_key AND terminal.retracted_at IS NULL
                 AND terminal.event_name IN (?, ?)
             )""",
        (PENDING_EVENT, fact.subject_key, RESULT_EVENT, RETIREMENT_EVENT),
    ).fetchone()
    return row is not None


def _matching_facts(db, workspace: Path, paths: Sequence[Path], commit: str) -> list[CarryInFact]:
    """Find cases for just-saved targets from direct installed-skill roots only."""
    from core.lesson_files import read_trial
    from core.skill_data_conversion import installed_skill_files
    from tools.filesystem_gate import canonical_target_path

    workspace_root = workspace.resolve(strict=False)
    saved = {canonical_target_path(str(path.resolve()), workspace_root) for path in paths}
    facts: list[CarryInFact] = []
    for source_path in installed_skill_files(workspace_root, db):
        try:
            trial = read_trial(source_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if trial is None or trial.lesson.state != "trial" or not trial.lesson.case:
            continue
        actions = trial.lesson.case.get("actions")
        target = trial.lesson.new_target_path if trial.lesson.target_file == "new" else trial.lesson.target_file
        if actions is None or not target or canonical_target_path(target, workspace_root) not in saved:
            continue
        canonical_target = canonical_target_path(target, workspace_root)
        if _is_thread_skill_target(canonical_target):
            record_source_skipped(
                db, lesson_id=trial.lesson.id, owner_thread_id=trial.lesson.owner,
                target_commit=commit, target_path=canonical_target,
                reason="thread_skill_no_unlineaged_route",
            )
            continue
        try:
            relative = source_path.resolve().relative_to(workspace_root).as_posix()
        except ValueError:
            record_source_skipped(
                db, lesson_id=trial.lesson.id, owner_thread_id=trial.lesson.owner,
                target_commit=commit, target_path=canonical_target,
            )
            logger.warning("carry-in case source is outside workspace: source={} workspace={}", source_path, workspace_root)
            continue
        facts.append(CarryInFact(
            trial.lesson.id, trial.lesson.owner, commit, canonical_target,
            f"{relative}#corsia.lesson.case",
        ))
    return facts


def _case_for_fact(workspace: Path, fact: CarryInFact) -> dict[str, Any] | None:
    """Re-read one lesson's own case; outcome facts intentionally contain no case text."""
    from core.lesson_files import read_trial

    relative, marker, field = fact.case_ref.partition("#")
    if marker != "#" or field != "corsia.lesson.case":
        return None
    try:
        trial = read_trial((workspace / relative).read_text(encoding="utf-8"))
    except OSError:
        return None
    if trial is None or trial.lesson.id != fact.lesson_id or trial.lesson.case is None:
        return None
    return trial.lesson.case


def _fresh_scope(db, workspace: Path, target_path: str):
    """Map a canonical target to its existing owner/project delivery scope."""
    from eval.fresh_turn import FreshTurnScope

    owner = target_scope_owner(db, workspace, target_path)
    row = None if owner is None else db.execute(
        "SELECT project_id FROM threads WHERE id=?", (owner,),
    ).fetchone()
    project_id = None if row is None or row[0] is None else str(row[0])
    return FreshTurnScope(project_id=project_id, owner_thread_id=owner, carry_in=True)


def _rebaseline_first(
    db, workspace: Path, fact: CarryInFact, actions: dict[str, Any], adapter: TraceAdapter, runs: list[CarryInRun],
) -> bool:
    """Re-run the prior blob on a changed local identity before judging this edit."""
    if fact.role == "rebaseline":
        return True
    from infra.database import write_transaction

    current_identity = getattr(adapter, "current_model_identity", lambda: None)()
    baseline = baseline_result(db, fact)
    setter = getattr(adapter, "set_fenced_target", None)
    if current_identity is None or baseline is None or setter is None or baseline[1] == current_identity:
        return True
    prior, _identity = baseline
    blob, reason = _recorded_target_blob(workspace, prior)
    if blob is None:
        with write_transaction(db, scope="carry_in_pending") as transaction:
            record_pending(
                transaction,
                CarryInFact(prior.lesson_id, prior.owner_thread_id, prior.target_commit, prior.target_path, prior.case_ref, "rebaseline"),
                reason=reason,
            )
        return False
    from eval.fresh_turn_context import FencedTarget
    rebaseline = CarryInFact(
        prior.lesson_id, prior.owner_thread_id, prior.target_commit, prior.target_path, prior.case_ref, "rebaseline",
    )
    setter(FencedTarget(prior.target_path, prior.target_commit, blob, rebaseline.role))
    import hashlib
    prior_case = _case_for_fact(workspace, prior)
    try:
        run = run_three(
            actions, adapter, scenario=scenario_for_case(prior_case or {}),
            before_sample=lambda: (hashlib.sha256(blob).hexdigest(), None),
        )
        with write_transaction(db, scope="carry_in_rebaseline") as transaction:
            if run.state == "settled":
                record_result(transaction, rebaseline, run, case_sha=_case_sha(prior_case))
            elif run.reason is not None:
                record_pending(transaction, rebaseline, reason=run.reason)
        runs.append(run)
        return run.state == "settled"
    finally:
        cleanup = getattr(adapter, "cleanup", None)
        if cleanup is not None:
            cleanup()


def drain_facts(db, workspace: Path, facts: Sequence[CarryInFact], adapter: TraceAdapter) -> list[CarryInRun]:
    """Use one caller-admitted local adapter to settle pending facts serially."""
    from infra.database import write_transaction

    runs: list[CarryInRun] = []
    for fact in facts:
        # Pending must survive a stalled or unavailable local-admission check.
        with write_transaction(db, scope="carry_in_pending") as transaction:
            record_pending(transaction, fact)
        if not is_pending(db, fact):
            continue
        case = _case_for_fact(workspace, fact)
        actions = None if case is None else case.get("actions")
        if not isinstance(actions, dict):
            continue
        if not _rebaseline_first(db, workspace, fact, actions, adapter, runs):
            continue
        setter = getattr(adapter, "set_fenced_target", None)
        if setter is not None:
            blob, reason = _recorded_target_blob(workspace, fact)
            if blob is None:
                with write_transaction(db, scope="carry_in_pending") as transaction:
                    record_pending(transaction, fact, reason=reason)
                continue
            from eval.fresh_turn_context import FencedTarget

            setter(FencedTarget(fact.target_path, fact.target_commit, blob, fact.role))
        run = run_three(
            actions, adapter, scenario=scenario_for_case(case),
            before_sample=lambda: _target_snapshot(workspace, fact),
        )
        try:
            with write_transaction(db, scope="carry_in_result") as transaction:
                if run.state == "pending" and run.reason is not None:
                    record_pending(transaction, fact, reason=run.reason)
                elif run.state == "settled":
                    owner = target_scope_owner(transaction, workspace, fact.target_path)
                    case_sha = _case_sha(case)
                    case_status = "proven" if run.verdict == "pass" else prior_case_status(transaction, fact, case_sha)
                    record_result(
                        transaction, fact, run, owner_unresolved=owner is None,
                        case_sha=case_sha, case_status=case_status,
                    )
                    if (
                        run.verdict == "fail" and case_status == "proven"
                        and (event := result_event(transaction, fact)) is not None
                    ):
                        from core.self_caught_corrections import record_carry_in_regression

                        record_carry_in_regression(
                            transaction, result_event_id=str(event[0]), thread_id=owner, created_at=str(event[1]),
                        )
            runs.append(run)
        finally:
            cleanup = getattr(adapter, "cleanup", None)
            if cleanup is not None:
                cleanup()
    return runs


from core.lesson_carry_in_drain import (
    _drain_committed_facts,
    _pending_facts,
    record_committed_save,
    schedule_committed_save,
    schedule_pending_drain,
)
