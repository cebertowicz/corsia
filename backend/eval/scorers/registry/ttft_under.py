from __future__ import annotations

from typing import Any

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState

from ._events import get_turn_attrs


def _evaluate_ttft_turns(
    turns: list[dict[str, Any]], start_i: int, end_i: int, bound: float,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    evaluated: list[dict[str, Any]] = []
    skips: list[str] = []
    failures: list[str] = []
    for one_based in range(start_i, end_i + 1):
        zero_based = one_based - 1
        if zero_based < 0 or zero_based >= len(turns):
            continue
        turn = turns[zero_based]
        ttft = turn.get("ttft_ms")
        if not isinstance(ttft, (int, float)):
            skips.append(f"turn {one_based}: no ttft_ms attribute")
            continue
        ttft_f = float(ttft)
        evaluated.append({"turn": one_based, "ttft_ms": ttft_f})
        if ttft_f > bound:
            failures.append(
                f"turn {one_based}: ttft_ms={ttft_f:.1f} > bound={bound:.1f}",
            )
    return evaluated, failures, skips


@scorer(metrics=[accuracy(), mean(), stderr()])
def ttft_under(
    *,
    bound: float,
    turn_range: list[int] | None = None,
    skip_first_turn: bool = False,
):

    if bound <= 0:
        raise ValueError(f"ttft_under: bound must be positive, got {bound}")

    async def score(state: TaskState, target: Target) -> Score:
        turns = get_turn_attrs(state)
        if not turns:
            return Score(
                value=INCORRECT,
                answer="no_data",
                explanation=(
                    "ttft_under: no turn_attrs in state.store. "
                    "Did the scenario run in Mode B?"
                ),
                metadata={"bound": bound, "turn_count": 0},
            )

        if turn_range and len(turn_range) >= 2:
            start_i, end_i = int(turn_range[0]), int(turn_range[1])
        else:
            start_i = 2 if skip_first_turn else 1
            end_i = len(turns)

        evaluated, failures, skips = _evaluate_ttft_turns(
            turns, start_i, end_i, bound,
        )

        if not evaluated:
            return Score(
                value=INCORRECT,
                answer="no_ttft_data",
                explanation=(
                    f"ttft_under: no turn carried a ttft_ms attribute. "
                    f"Streaming likely off or instrumentation not reached. "
                    f"Skips: {skips}"
                ),
                metadata={"bound": bound, "skips": skips},
            )

        passed = not failures
        if passed:
            worst = max(e["ttft_ms"] for e in evaluated)
            explanation = (
                f"ttft_under PASS: worst ttft={worst:.1f}ms ≤ bound={bound:.1f}ms "
                f"across {len(evaluated)} turns."
            )
        else:
            explanation = "ttft_under FAIL:\n" + "\n".join(failures)

        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=f"{len(evaluated) - len(failures)}/{len(evaluated)} ≤ {bound}ms",
            explanation=explanation,
            metadata={
                "bound": bound,
                "evaluated": evaluated,
                "skips": skips,
                "failures": failures,
                "turn_count": len(turns),
            },
        )

    return score


__all__ = ["ttft_under"]
