from __future__ import annotations

import os

from inspect_ai import Task, task
from inspect_ai.scorer import (
    accuracy, mean, model_graded_qa, stderr,
)

from eval.datasets import (
    build_ttft_burst_samples,
    build_throughput_sustain_samples,
    build_latency_tool_loops_samples,
    build_output_stability_samples,
    memory_dataset,
)
from eval.scorers import (
    seam_latency_scorer,
    null_tool_paired_scorer,
    structured_validity_scorer,
)
from eval.solvers import corsia_turn_solver


def _judge_model() -> str:
    judge = os.environ.get("CORSIA_EVAL_JUDGE_MODEL")
    if judge and judge.strip():
        return judge.strip()
    raise RuntimeError(
        "No eval judge model configured. Set CORSIA_EVAL_JUDGE_MODEL "
        "(e.g. a local oMLX judge or the gate's cloud judge). "
        "No hardcoded default model is used."
    )



@task
def corsia_ttft_at_burst() -> Task:
    return Task(
        dataset=memory_dataset(
            build_ttft_burst_samples(),
            name="corsia_ttft_at_burst",
        ),
        solver=corsia_turn_solver(),
        scorer=null_tool_paired_scorer(),  # repurposed: records ms_total
    )


@task
def corsia_throughput_at_sustain() -> Task:
    return Task(
        dataset=memory_dataset(
            build_throughput_sustain_samples(),
            name="corsia_throughput_at_sustain",
        ),
        solver=corsia_turn_solver(),
        scorer=null_tool_paired_scorer(),  # records ms_total + usage
    )


@task
def corsia_latency_under_tool_loops() -> Task:
    return Task(
        dataset=memory_dataset(
            build_latency_tool_loops_samples(
                tool_loop_count=5,
                expected_seam_budget_ms=100.0,
                paired=True,
            ),
            name="corsia_latency_under_tool_loops",
        ),
        solver=corsia_turn_solver(
            extra_metadata={"tools_in_scope": ["null_probe"]},
        ),
        scorer=[
            seam_latency_scorer(budget_ms=100.0),
            null_tool_paired_scorer(),
        ],
    )


@task
def corsia_output_stability() -> Task:
    return Task(
        dataset=memory_dataset(
            build_output_stability_samples(n_reruns=5),
            name="corsia_output_stability",
        ),
        solver=corsia_turn_solver(),
        scorer=[
            structured_validity_scorer(mode="json"),
            model_graded_qa(model=_judge_model()),
            null_tool_paired_scorer(),
        ],
    )


__all__ = [
    "corsia_ttft_at_burst",
    "corsia_throughput_at_sustain",
    "corsia_latency_under_tool_loops",
    "corsia_output_stability",
]
