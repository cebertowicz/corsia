from __future__ import annotations

from inspect_ai import Task, task

from eval.scorers.registry.workflow_quality import workflow_quality

from ._common import build_workflow_dataset, load_rubric, workflow_scenario_solver


@task
def corsia_error_recovery(
    *,
    judge_model: str | None = None,
    threshold: float = 0.7,
) -> Task:
    rubric = load_rubric("error_recovery")
    return Task(
        dataset=build_workflow_dataset("error_recovery"),
        solver=workflow_scenario_solver(),
        scorer=workflow_quality(
            rubric=rubric,
            model=judge_model,
            threshold=threshold,
        ),
    )


__all__ = ["corsia_error_recovery"]
