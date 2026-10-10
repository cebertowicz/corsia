from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from inspect_ai.log import EvalLog, write_eval_log
from inspect_ai.log._log import (
    EvalSpec, EvalSample, EvalDataset, EvalResults, EvalStats,
)
from inspect_ai._eval.eval import EvalConfig
from inspect_ai.scorer import Score, CORRECT, INCORRECT, NOANSWER

from eval.solvers.yaml_scenario_types import ScenarioResult, ScenarioStatus


DEFAULT_INSPECT_LOG_DIR = Path.home() / ".corsia" / "evals" / "runs"


def _status_to_log_status(scenario_status: str) -> str:
    if scenario_status == ScenarioStatus.ERROR:
        return "error"
    return "success"


def _score_value(passed: bool, raw_value: Any) -> str | float | int | bool:
    if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
        return float(raw_value)
    return CORRECT if passed else INCORRECT


def _build_sample(result: ScenarioResult) -> EvalSample:
    scores: dict[str, Score] = {}
    for name, outcome in result.scorer_outcomes.items():
        scores[name] = Score(
            value=_score_value(outcome.passed, outcome.raw_value),
            answer=str(outcome.raw_value) if outcome.raw_value is not None else None,
            explanation=outcome.explanation,
            metadata={
                "passed": outcome.passed,
                "raw_value_type": type(outcome.raw_value).__name__,
            },
        )

    if result.blocked_scorers:
        scores["__blocked__"] = Score(
            value=NOANSWER,
            answer=",".join(result.blocked_scorers),
            explanation=(
                f"Blocked: {len(result.blocked_scorers)} scorer(s) unimplemented "
                f"({result.blocked_scorers!r})."
            ),
            metadata={"blocked_scorers": result.blocked_scorers},
        )
    if result.unknown_scorers:
        scores["__unknown__"] = Score(
            value=NOANSWER,
            answer=",".join(result.unknown_scorers),
            explanation=(
                f"Unknown: {len(result.unknown_scorers)} scorer(s) not in registry "
                f"({result.unknown_scorers!r})."
            ),
            metadata={"unknown_scorers": result.unknown_scorers},
        )

    metadata: dict[str, Any] = {
        "scenario_status": result.status,
        "mode": result.mode,
        "turn_count": len(result.turn_attrs),
        "errors": result.errors,
        "blocked_scorers": result.blocked_scorers,
        "unknown_scorers": result.unknown_scorers,
    }
    if result.turn_attrs:
        metadata["turn_attrs"] = result.turn_attrs

    sample = EvalSample(
        id=result.scenario_id,
        epoch=1,
        input=str(result.endpoint_response.get("content") or ""),
        target=str(result.scenario_id),  # no canonical "answer" target — use scenario id
        scores=scores,
        metadata=metadata,
        error=None,
    )
    return sample


def write_scenario_eval_log(
    result: ScenarioResult,
    *,
    log_dir: Path | str | None = None,
    task_name: str = "corsia.yaml_scenario",
    model: str | None = None,
    run_id: str | None = None,
) -> Path:
    out_dir = Path(log_dir) if log_dir is not None else DEFAULT_INSPECT_LOG_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    rid = run_id or f"run-{uuid.uuid4().hex[:12]}"

    resolved_model = (
        model
        or (result.endpoint_response.get("model") if isinstance(result.endpoint_response, dict) else None)
        or "unknown"
    )

    now_iso = datetime.now().astimezone().isoformat()

    spec = EvalSpec(
        run_id=rid,
        created=now_iso,
        task=task_name,
        task_id=rid,
        dataset=EvalDataset(
            name="corsia_scenario",
            samples=1,
        ),
        model=resolved_model,
        config=EvalConfig(),
        metadata={
            "scenario_id": result.scenario_id,
            "scenario_mode": result.mode,
            "scenario_status": result.status,
        },
    )

    sample = _build_sample(result)
    samples = [sample]

    results = EvalResults(
        total_samples=1,
        completed_samples=1,
        metadata={
            "scenario_status": result.status,
            "scorer_pass_count": sum(
                1 for o in result.scorer_outcomes.values() if o.passed
            ),
            "scorer_fail_count": sum(
                1 for o in result.scorer_outcomes.values() if not o.passed
            ),
        },
    )

    stats = EvalStats(
        started_at=now_iso,
        completed_at=now_iso,
    )

    log = EvalLog(
        eval=spec,
        plan=log_plan_default(),
        results=results,
        stats=stats,
        samples=samples,
        status=_status_to_log_status(result.status),
    )

    out_path = out_dir / f"{rid}.eval"
    write_eval_log(log, str(out_path))
    return out_path


def log_plan_default():
    from inspect_ai.log._log import EvalPlan
    return EvalPlan()


__all__ = [
    "DEFAULT_INSPECT_LOG_DIR",
    "write_scenario_eval_log",
]
