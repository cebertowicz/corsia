from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from inspect_ai.solver import Generate, Solver, TaskState, solver

from eval.datasets import from_yaml_scenario, memory_dataset
from eval.solvers.yaml_scenario import YamlScenarioSolver


_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_RUBRIC_DIR = _FIXTURE_DIR / "rubrics"


def load_rubric(name: str) -> str:
    path = _RUBRIC_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"workflow rubric not found: {path}")
    return path.read_text(encoding="utf-8")


def load_scenario(name: str) -> dict[str, Any]:
    path = _FIXTURE_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"workflow scenario not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        scenario = yaml.safe_load(f)
    if not isinstance(scenario, dict):
        raise ValueError(f"workflow scenario {path} did not parse to a dict")
    return scenario


def build_workflow_sample(scenario_name: str) -> Any:
    scenario = load_scenario(scenario_name)
    scenario["scorers"] = []
    return from_yaml_scenario(scenario)


def build_workflow_dataset(scenario_name: str) -> Any:
    sample = build_workflow_sample(scenario_name)
    return memory_dataset([sample], name=f"corsia_{scenario_name}")


@solver
def workflow_scenario_solver(
    *,
    endpoint: str | None = None,
    token: str | None = None,
    timeout: float | None = None,
) -> Solver:
    inner = YamlScenarioSolver(endpoint=endpoint, token=token, timeout=timeout)

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample = _state_to_sample(state)
        result = await inner.run(sample)

        state.store.set("corsia_eval_response", dict(result.endpoint_response or {}))
        if result.turn_attrs:
            state.store.set("turn_attrs", list(result.turn_attrs))
        if result.turn_responses:
            state.store.set("turn_responses", list(result.turn_responses))

        final_content = ""
        if isinstance(result.endpoint_response, dict):
            final_content = str(result.endpoint_response.get("content") or "")
        state.output.completion = final_content

        state.metadata = dict(state.metadata or {})
        state.metadata["workflow_status"] = result.status.value if hasattr(result.status, "value") else str(result.status)
        if result.errors:
            state.metadata["workflow_errors"] = list(result.errors)

        return state

    return solve


class _DuckSample:

    def __init__(self, *, sid: str, inp: str, metadata: dict[str, Any]) -> None:
        self.id = sid
        self.input = inp
        self.metadata = metadata


def _state_to_sample(state: TaskState) -> _DuckSample:
    sid = str(getattr(state, "sample_id", None) or getattr(state, "id", "") or "workflow")
    inp = getattr(state, "input", "") or ""
    if not isinstance(inp, str):
        inp = str(inp)
    metadata = dict(state.metadata or {})
    return _DuckSample(sid=sid, inp=inp, metadata=metadata)


def build_solver() -> Any:
    return YamlScenarioSolver()


__all__ = [
    "load_rubric",
    "load_scenario",
    "build_workflow_sample",
    "build_workflow_dataset",
    "build_solver",
    "workflow_scenario_solver",
]
