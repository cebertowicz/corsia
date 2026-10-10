
from __future__ import annotations

from typing import Any

from .yaml_scenario_types import ScoreResult


def parse_scorer_decl(decl: Any) -> tuple[str, dict] | None:
    if isinstance(decl, str):
        return decl, {}
    if isinstance(decl, dict):
        name = decl.get("name")
        if not name:
            return None
        args = decl.get("args") or {}
        return name, dict(args)
    return None


def build_task_state(sample: Any, response: dict[str, Any]) -> Any:
    from inspect_ai.model import ChatMessageUser, ModelName
    from inspect_ai.solver import TaskState

    sample_id = getattr(sample, "id", None) or "unknown"
    inp = getattr(sample, "input", "") or ""
    metadata = dict(getattr(sample, "metadata", None) or {})

    messages = [ChatMessageUser(content=inp)] if inp else []

    state = TaskState(
        model=ModelName("eval/yaml-scenario-solver"),
        sample_id=sample_id,
        epoch=0,
        input=inp,
        messages=messages,
        metadata=metadata,
    )

    state.store.set("corsia_eval_response", response)

    state.output.completion = response.get("content") or ""

    return state


def build_target(sample: Any) -> Any:
    from inspect_ai.scorer import Target

    tgt = getattr(sample, "target", None)
    if tgt is None:
        return Target("")
    if isinstance(tgt, list):
        return Target([str(x) for x in tgt])
    return Target(str(tgt))


def normalize_score(name: str, score: Any) -> ScoreResult:
    from inspect_ai.scorer import CORRECT

    value = getattr(score, "value", None)
    explanation = getattr(score, "explanation", "") or ""

    if value == CORRECT or value is True:
        passed = True
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        passed = value > 0
    else:
        passed = False

    return ScoreResult(
        name=name,
        passed=passed,
        explanation=explanation,
        raw_value=value,
    )


__all__ = [
    "parse_scorer_decl",
    "build_task_state",
    "build_target",
    "normalize_score",
]
