from __future__ import annotations

from typing import Any

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState

from ._events import get_turn_attrs


def _collect_system_prompt_turns(
    turns: list[dict[str, Any]], start_i: int, end_i: int,
) -> list[dict[str, Any]]:
    evaluated: list[dict[str, Any]] = []
    for one_based in range(start_i, end_i + 1):
        zero_based = one_based - 1
        if zero_based < 0 or zero_based >= len(turns):
            continue
        turn = turns[zero_based]
        evaluated.append({
            "turn": one_based,
            "system_prompt_hash": turn.get("system_prompt_hash"),
            "system_prompt_size": turn.get("system_prompt_size"),
        })
    return evaluated


def _hash_stability_score(evaluated: list[dict[str, Any]], turns: list[dict[str, Any]]) -> Score:
    hashes = [e["system_prompt_hash"] for e in evaluated]
    non_null = [h for h in hashes if isinstance(h, str) and h]
    if not non_null:
        return Score(
            value=INCORRECT,
            answer="no_hash_captured",
            explanation=(
                "system_prompt_stable: no system_prompt_hash attribute "
                "captured on any evaluated turn. Context-build span "
                "instrumentation may be missing or eval mode isn't tracing."
            ),
            metadata={"evaluated": evaluated},
        )

    unique = set(non_null)
    if len(unique) > 1:
        return Score(
            value=INCORRECT,
            answer="drift",
            explanation=(
                f"system_prompt_stable FAIL: hash drifted across turns "
                f"({len(unique)} distinct values). "
                f"Distinct hashes (first 12 hex): "
                f"{sorted({h[:12] for h in unique})}"
            ),
            metadata={
                "evaluated": evaluated,
                "unique_hashes": sorted(unique),
                "turn_count": len(turns),
            },
        )

    return Score(
        value=CORRECT,
        answer="stable",
        explanation=(
            f"system_prompt_stable PASS: {len(non_null)} turns share hash "
            f"{next(iter(unique))[:12]}..."
        ),
        metadata={
            "evaluated": evaluated,
            "unique_hash": next(iter(unique)),
            "turn_count": len(turns),
        },
    )


@scorer(metrics=[accuracy(), mean(), stderr()])
def system_prompt_stable(*, turn_range: list[int] | None = None):

    async def score(state: TaskState, target: Target) -> Score:
        turns = get_turn_attrs(state)
        if not turns:
            return Score(
                value=INCORRECT,
                answer="no_data",
                explanation=(
                    "system_prompt_stable: no turn_attrs in state.store. "
                    "Did the scenario run in Mode B?"
                ),
                metadata={"turn_count": 0},
            )

        if turn_range and len(turn_range) >= 2:
            start_i, end_i = int(turn_range[0]), int(turn_range[1])
        else:
            start_i, end_i = 1, len(turns)

        evaluated = _collect_system_prompt_turns(turns, start_i, end_i)

        if not evaluated:
            return Score(
                value=INCORRECT,
                answer="no_evaluated_turns",
                explanation=(
                    f"system_prompt_stable: turn_range={turn_range} yielded no eligible turns "
                    f"(total turns={len(turns)})."
                ),
                metadata={"turn_count": len(turns)},
            )

        return _hash_stability_score(evaluated, turns)

    return score


__all__ = ["system_prompt_stable"]
