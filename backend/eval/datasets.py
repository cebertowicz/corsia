from __future__ import annotations

import uuid
from typing import Any, Iterable

from inspect_ai.dataset import MemoryDataset, Sample



def build_ttft_burst_samples(*, model: str | None = None) -> list[Sample]:
    samples: list[Sample] = []
    for n in (1, 4):
        for cache in ("cold", "warm"):
            for i in range(n):
                samples.append(Sample(
                    id=f"ttft_n{n}_{cache}_{i}",
                    input="Reply with exactly the word 'pong' and nothing else.",
                    metadata={
                        "scenario": f"ttft_at_burst_n{n}_{cache}_{i}",
                        "mode": "ttft_burst",
                        "burst_n": n,
                        "cache_state": cache,
                        "model": model,
                        "params": {"temperature": 0.0, "max_tokens": 16},
                    },
                ))
    return samples


def build_throughput_sustain_samples(*, model: str | None = None) -> list[Sample]:
    return [
        Sample(
            id="throughput_1k_long",
            input=(
                "Write a 1000-word fictional account of a small mountain town "
                "preparing for a winter festival. Include vivid sensory detail "
                "and at least three named characters. Aim for ~1000 words; "
                "do not abbreviate."
            ),
            metadata={
                "scenario": "throughput_at_sustain_1k_target",
                "mode": "throughput_sustain",
                "model": model,
                "params": {"temperature": 0.7, "max_tokens": 1500},
            },
        ),
    ]


def build_latency_tool_loops_samples(
    *,
    tool_loop_count: int = 5,
    expected_seam_budget_ms: float = 100.0,
    model: str | None = None,
    paired: bool = True,
) -> list[Sample]:
    samples: list[Sample] = []

    direct_id = f"latency_tool_loops_n{tool_loop_count}"
    samples.append(Sample(
        id=direct_id,
        input=(
            f"Call the null_probe tool {tool_loop_count} times in sequence, "
            f"each time with a unique probe_id (e.g. 'p1', 'p2', ...). "
            f"After the final call returns, reply with 'done'."
        ),
        metadata={
            "scenario": direct_id,
            "mode": "latency_tool_loops",
            "tool_loop_count": tool_loop_count,
            "expected_seam_budget_ms": expected_seam_budget_ms,
            "tools_in_scope": ["null_probe"],
            "model": model,
        },
    ))

    if paired:
        pair_id = uuid.uuid4().hex[:8]
        samples.append(Sample(
            id=f"seam_paired_real_{pair_id}",
            input="Briefly describe what a mountain looks like in winter (1-2 sentences).",
            metadata={
                "scenario": "seam_paired_real",
                "mode": "seam_paired_real",
                "pair_id": pair_id,
                "model": model,
            },
        ))
        samples.append(Sample(
            id=f"seam_paired_null_{pair_id}",
            input=(
                "Briefly describe what a mountain looks like in winter "
                "(1-2 sentences). After your description, call the null_probe tool "
                f"once with probe_id='paired_{pair_id}'."
            ),
            metadata={
                "scenario": "seam_paired_null",
                "mode": "seam_paired_null",
                "pair_id": pair_id,
                "tools_in_scope": ["null_probe"],
                "model": model,
            },
        ))

    return samples


def build_output_stability_samples(
    *,
    n_reruns: int = 5,
    model: str | None = None,
) -> list[Sample]:
    samples: list[Sample] = []
    base_prompt = (
        "Reply with valid JSON of the form "
        '{"answer": "<one sentence>", "confidence": <0..1>}. '
        "Question: What color is the sky on a clear day?"
    )
    for i in range(n_reruns):
        samples.append(Sample(
            id=f"output_stability_rerun_{i}",
            input=base_prompt,
            target='{"answer":"blue","confidence":1.0}',
            metadata={
                "scenario": f"output_stability_rerun_{i}",
                "mode": "output_stability",
                "rerun_idx": i,
                "model": model,
                "seed": 42,
                "params": {"temperature": 0.0, "max_tokens": 64},
            },
        ))
    return samples



def from_yaml_scenario(scenario: dict[str, Any]) -> Sample:
    sid = scenario.get("id") or f"yaml_{uuid.uuid4().hex[:8]}"
    inp = scenario.get("input")
    if (
        inp is None
        and not scenario.get("steps")
        and not scenario.get("turns")
    ):
        raise ValueError(
            f"scenario {sid} missing 'input' "
            "(and no 'steps' / 'turns' fallback)",
        )

    metadata = dict(scenario.get("metadata") or {})

    if scenario.get("mode") is not None and "mode" not in metadata:
        metadata["mode"] = scenario["mode"]

    if scenario.get("setup") is not None and "setup" not in metadata:
        metadata["setup"] = scenario["setup"]

    scorers = scenario.get("scorers")
    if scorers is not None:
        metadata["scorers"] = scorers

    if scenario.get("steps") and "steps" not in metadata:
        metadata["steps"] = scenario["steps"]
    if scenario.get("turns") and "turns" not in metadata:
        metadata["turns"] = scenario["turns"]
    if scenario.get("pre_conditions") and "pre_conditions" not in metadata:
        metadata["pre_conditions"] = scenario["pre_conditions"]
    if scenario.get("expected_outcomes") and "expected_outcomes" not in metadata:
        metadata["expected_outcomes"] = scenario["expected_outcomes"]
    if scenario.get("pass_if") and "pass_if" not in metadata:
        metadata["pass_if"] = scenario["pass_if"]

    target = scenario.get("target")
    sample_kwargs: dict[str, Any] = {
        "id": sid,
        "input": inp if inp is not None else "",
        "metadata": metadata,
    }
    if target is not None:
        sample_kwargs["target"] = target
    return Sample(**sample_kwargs)


def memory_dataset(samples: Iterable[Sample], name: str = "corsia_eval") -> MemoryDataset:
    return MemoryDataset(samples=list(samples), name=name)


__all__ = [
    "build_ttft_burst_samples",
    "build_throughput_sustain_samples",
    "build_latency_tool_loops_samples",
    "build_output_stability_samples",
    "from_yaml_scenario",
    "memory_dataset",
]
