"""File-local carry-in checks for lesson guidance.

A lesson keeps its existing case as the source of truth.  When it supplies an
optional action contract, this module scores a fresh turn's canonical tool calls
mechanically; it neither imports the disposable eval harness nor chooses or
loads a model.
"""
from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

from loguru import logger

Action = dict[str, Any]
Verdict = Literal["pass", "fail"]
T = TypeVar("T")

ACTION_FIELDS = frozenset({"tool", "params"})
ACTIONS_FIELDS = frozenset({"required_actions", "forbidden_actions", "decisive_action"})
SAMPLE_COUNT = 3
PASS_THRESHOLD = 2
# Set only after the first guarded three-sample measurement; never infer a number.
CASES_PER_EDIT_CAP: int | None = None
LOCAL_SELECTOR = "omlx@ns-f378151a51/Qwen3.8-Flash-Next-MLX-oQ6-MTP"


class TraceAdapter(Protocol):
    """The owned boundary to the local fresh-turn runner."""

    def ready(self) -> bool:
        """Whether an already-loaded local model may receive a turn now."""

    def run(self, *, target_path: str, case: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        """Return canonical ``EvalTurnResult.tool_calls`` for one fresh turn."""


class LocalTraceAdapter:
    """Admission-gated bridge to the one shared product fresh-turn runner."""

    def __init__(self, user_id: str, runner: Callable[..., Any] | None = None):
        self.user_id = user_id
        self._runner = runner

    def ready(self) -> bool:
        from llm.capabilities import get_active_local_threads, snapshot_active_threads
        from llm.catalog import get_model, get_model_by_litellm

        model = get_model(LOCAL_SELECTOR) or get_model_by_litellm(LOCAL_SELECTOR)
        return bool(model and model.get("status") == "ready" and not get_active_local_threads(snapshot_active_threads()))

    def run(self, *, target_path: str, case: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        from eval.fresh_turn import run_fresh_turn

        fresh = asyncio.run((self._runner or run_fresh_turn)(
            user_id=self.user_id,
            prompt=json.dumps({"target_path": target_path, "case": case}, sort_keys=True),
            model=LOCAL_SELECTOR,
            timeout_seconds=120.0,
        ))
        return () if fresh.result is None else fresh.result.tool_calls


@dataclass(frozen=True)
class TraceScore:
    """The first decisive or forbidden call's mechanical verdict."""

    verdict: Verdict
    reason: str
    decisive_call_index: int | None


@dataclass(frozen=True)
class CarryInRun:
    """Three samples, or a pending result when local admission is unavailable."""

    state: Literal["pending", "settled"]
    scores: tuple[TraceScore, ...] = ()

    @property
    def verdict(self) -> Verdict | None:
        if self.state != "settled":
            return None
        return "pass" if sum(score.verdict == "pass" for score in self.scores) >= PASS_THRESHOLD else "fail"


def _json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _action(value: object, *, field: str) -> Action:
    if not isinstance(value, dict) or set(value) != ACTION_FIELDS:
        raise ValueError(f"corsia.lesson.case.actions.{field} actions must be {{tool, params}}")
    tool, params = value.get("tool"), value.get("params")
    if not isinstance(tool, str) or not tool.strip() or not isinstance(params, dict) or not _json_value(params):
        raise ValueError(f"corsia.lesson.case.actions.{field} actions need a tool and JSON-object params")
    return {"tool": tool.strip(), "params": params}


def validate_actions(value: object) -> dict[str, Any] | None:
    """Validate the intentionally small, file-local action contract.

    It deliberately excludes evaluator bindings, shell semantics and allowances:
    a lesson needing those remains a case without an executable carry-in test.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != ACTIONS_FIELDS:
        raise ValueError(
            "corsia.lesson.case.actions must name required_actions, forbidden_actions and decisive_action"
        )
    required_raw, forbidden_raw = value.get("required_actions"), value.get("forbidden_actions")
    if not isinstance(required_raw, list) or not required_raw or not isinstance(forbidden_raw, list):
        raise ValueError("corsia.lesson.case.actions requires non-empty required_actions and list forbidden_actions")
    required = [_action(item, field="required_actions") for item in required_raw]
    forbidden = [_action(item, field="forbidden_actions") for item in forbidden_raw]
    decisive = _action(value.get("decisive_action"), field="decisive_action")
    if decisive not in required:
        raise ValueError("corsia.lesson.case.actions.decisive_action must be one declared required action")
    return {
        "required_actions": required,
        "forbidden_actions": forbidden,
        "decisive_action": decisive,
    }


def _parsed_params(value: object) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def canonical_calls(calls: Sequence[Mapping[str, Any]]) -> list[Action]:
    """Normalize the canonical eval projection, including its generic tool wrapper."""
    normalized: list[Action] = []
    for call in calls:
        name = call.get("name") or call.get("tool")
        params = _parsed_params(call.get("arguments", call.get("params", {})))
        if not isinstance(name, str) or params is None:
            continue
        if name == "tool_call":
            inner_name, inner_params = params.get("name"), _parsed_params(params.get("arguments"))
            if not isinstance(inner_name, str) or inner_params is None:
                continue
            name, params = inner_name, inner_params
        normalized.append({"tool": name, "params": params})
    return normalized


def _subset(expected: object, actual: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _subset(value, actual[key]) for key, value in expected.items())
    return expected == actual


def action_matches(call: Mapping[str, Any], action: Mapping[str, Any]) -> bool:
    """Match an action's params as a subset of one canonical call's params."""
    return call.get("tool") == action.get("tool") and _subset(action.get("params", {}), call.get("params", {}))


def score_trace(actions: Mapping[str, Any], calls: Sequence[Mapping[str, Any]]) -> TraceScore:
    """Score a settled fresh turn by its first forbidden or decisive call."""
    contract = validate_actions(dict(actions))
    assert contract is not None
    required = contract["required_actions"]
    decisive = contract["decisive_action"]
    preconditions = [action for action in required if action != decisive]
    normalized = canonical_calls(calls)
    for index, call in enumerate(normalized):
        if any(action_matches(call, action) for action in contract["forbidden_actions"]):
            return TraceScore("fail", "forbidden decisive action", index)
        if action_matches(call, decisive):
            seen = normalized[:index]
            if all(any(action_matches(previous, action) for previous in seen) for action in preconditions):
                return TraceScore("pass", "required decisive action", index)
            return TraceScore("fail", "required precondition missing before decisive action", index)
    return TraceScore("fail", "no_decisive_action", None)


def run_three(
    actions: Mapping[str, Any], adapter: TraceAdapter, *, target_path: str = "", case: Mapping[str, Any] | None = None,
) -> CarryInRun:
    """Run exactly three serialized fresh turns, or retain pending when not ready."""
    if not adapter.ready():
        return CarryInRun("pending")
    scores = tuple(
        score_trace(actions, adapter.run(target_path=target_path, case=case or {}))
        for _ in range(SAMPLE_COUNT)
    )
    return CarryInRun("settled", scores)


def is_discriminating(before: CarryInRun, after: CarryInRun) -> bool | None:
    """Promotion requires a 2-of-3 pre-lesson failure and post-lesson pass."""
    if before.state == "pending" or after.state == "pending":
        return None
    return before.verdict == "fail" and after.verdict == "pass"


def rotated_cases(cases: Sequence[T], *, cap: int | None, edit_number: int) -> list[T]:
    """Select a stable rotating window; an unset cap intentionally runs nothing."""
    if cap is None or cap <= 0 or not cases:
        return []
    size = min(cap, len(cases))
    start = (edit_number * size) % len(cases)
    return [cases[(start + offset) % len(cases)] for offset in range(size)]


PENDING_EVENT = "learning_loop.carry_in_pending"
RESULT_EVENT = "learning_loop.carry_in_result"
RETIREMENT_EVENT = "learning_loop.carry_in_retirement"


@dataclass(frozen=True)
class CarryInFact:
    """A queryable check identity; target text never enters outcome facts."""

    lesson_id: str
    owner_thread_id: str
    target_commit: str
    target_path: str
    case_ref: str

    @property
    def subject_key(self) -> str:
        return f"carry-in:{self.lesson_id}:{self.target_commit}"


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
        "lesson_id": id_attr(), "target_commit": id_attr(), "target_path": id_attr(), "case_ref": id_attr(),
        "verdict": enum_attr("pass", "fail"), "owner_resolution": enum_attr("resolved", "owner_unresolved"), "sample_count": count_attr(),
        "passed_samples": count_attr(), "reason": text_attr(500),
    }
    result = record_outcome_event(
        db, dedupe_key=f"{fact.subject_key}:{dedupe_suffix}", event_name=event_name,
        source="corsia", provenance="instrumented", ts=_now(), subject_key=fact.subject_key,
        project_id=_project_id(db, fact.owner_thread_id), agent_thread_id=fact.owner_thread_id,
        attributes={"lesson_id": fact.lesson_id, "target_commit": fact.target_commit,
                    "target_path": fact.target_path, "case_ref": fact.case_ref, **attributes}, declared_specs=specs,
    )
    return result.inserted


def record_pending(db, fact: CarryInFact) -> bool:
    """Record a local-model-unavailable carry-in check once, without a durable queue."""
    return _record(db, event_name=PENDING_EVENT, fact=fact, dedupe_suffix="pending", attributes={})


def record_result(db, fact: CarryInFact, run: CarryInRun, *, owner_unresolved: bool = False) -> bool:
    """Persist a settled three-sample result; callers keep pending results pending."""
    if run.state != "settled" or run.verdict is None or len(run.scores) != SAMPLE_COUNT:
        raise ValueError("only a settled three-sample carry-in run has a result")
    passed = sum(score.verdict == "pass" for score in run.scores)
    return _record(
        db, event_name=RESULT_EVENT, fact=fact, dedupe_suffix="result",
        attributes={"verdict": run.verdict, "owner_resolution": "owner_unresolved" if owner_unresolved else "resolved", "sample_count": SAMPLE_COUNT, "passed_samples": passed},
    )


def _result_event(db, fact: CarryInFact):
    return db.execute(
        "SELECT id,ts FROM outcome_events WHERE dedupe_key=? AND retracted_at IS NULL",
        (f"{fact.subject_key}:result",),
    ).fetchone()


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

    saved = {canonical_target_path(str(path.resolve()), workspace) for path in paths}
    facts: list[CarryInFact] = []
    for source_path in installed_skill_files(workspace, db):
        try:
            trial = read_trial(source_path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if trial is None or trial.lesson.state != "trial" or not trial.lesson.case:
            continue
        actions = trial.lesson.case.get("actions")
        target = trial.lesson.new_target_path if trial.lesson.target_file == "new" else trial.lesson.target_file
        if actions is None or not target or canonical_target_path(target, workspace) not in saved:
            continue
        try:
            relative = source_path.resolve().relative_to(workspace).as_posix()
        except ValueError:
            continue
        facts.append(CarryInFact(
            trial.lesson.id, trial.lesson.owner, commit, canonical_target_path(target, workspace),
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


def drain_facts(db, workspace: Path, facts: Sequence[CarryInFact], adapter: TraceAdapter) -> list[CarryInRun]:
    """Use one caller-admitted local adapter to settle pending facts serially."""
    runs: list[CarryInRun] = []
    for fact in facts:
        record_pending(db, fact)
        if not is_pending(db, fact):
            continue
        case = _case_for_fact(workspace, fact)
        actions = None if case is None else case.get("actions")
        if not isinstance(actions, dict):
            continue
        run = run_three(actions, adapter, target_path=fact.target_path, case=case)
        if run.state == "settled":
            owner = target_scope_owner(db, workspace, fact.target_path)
            record_result(db, fact, run, owner_unresolved=owner is None)
            if run.verdict == "fail" and (event := _result_event(db, fact)) is not None:
                from core.self_caught_corrections import record_carry_in_regression

                record_carry_in_regression(
                    db, result_event_id=str(event[0]), thread_id=owner, created_at=str(event[1]),
                )
        runs.append(run)
    return runs


_DRAIN_LOCK = threading.Lock()


def record_committed_save(workspace: Path, paths: Sequence[Path], commit: str) -> None:
    """Start the single non-blocking in-process drain after a guidance commit.

    The outcome event is the durable pending signal; this transient drain is not
    a queue and deliberately makes no model call until its owned adapter exists.
    """
    thread = threading.Thread(
        target=_record_save_drain, args=(workspace, tuple(paths), commit), daemon=True,
        name="corsia-carry-in",
    )
    thread.start()


def _owner_user_id(db, thread_id: str) -> str | None:
    row = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _drain_committed_facts(
    db, workspace: Path, paths: Sequence[Path], commit: str,
    *, adapter_factory: Callable[[str], TraceAdapter] = LocalTraceAdapter,
) -> None:
    """Advance a committed edit through the sole local adapter when its owner is known."""
    for fact in _matching_facts(db, workspace, paths, commit):
        user_id = _owner_user_id(db, fact.owner_thread_id)
        if user_id is None:
            record_pending(db, fact)
            continue
        drain_facts(db, workspace, [fact], adapter_factory(user_id))


def _record_save_drain(workspace: Path, paths: Sequence[Path], commit: str) -> None:
    from infra.database import get_db

    with _DRAIN_LOCK:
        db = None
        try:
            db = get_db()
            _drain_committed_facts(db, workspace, paths, commit)
            db.commit()
        except Exception:
            # The durable save already landed; a later edit can retry this transient drain.
            logger.opt(exception=True).warning("carry-in pending fact could not be recorded")
        finally:
            if db is not None:
                db.close()
