from __future__ import annotations

from typing import Any

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState

from ._events import get_turn_attrs


def _ratio_for_turn(turn: dict[str, Any]) -> tuple[float | None, int, int]:
    cached = turn.get("cached_tokens")
    new = turn.get("prefill_new_tokens")
    if isinstance(cached, (int, float)) and isinstance(new, (int, float)):
        total = int(cached) + int(new)
        if total == 0:
            return None, 0, 0
        return int(cached) / total, int(cached), int(new)

    usage_cached = turn.get("cached_tokens_usage")
    prompt_tokens = turn.get("prompt_tokens")
    if isinstance(usage_cached, (int, float)) and isinstance(prompt_tokens, (int, float)):
        prompt_int = int(prompt_tokens)
        cached_int = int(usage_cached)
        new_int = max(0, prompt_int - cached_int)
        total = prompt_int
        if total == 0:
            return None, 0, 0
        return cached_int / total, cached_int, new_int

    return None, 0, 0


def _evaluate_hit_rate_turns(
    turns: list[dict[str, Any]], start_i: int, end_i: int, bound: float,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    evaluated: list[dict[str, Any]] = []
    failures: list[str] = []
    skips: list[str] = []
    for one_based in range(start_i, end_i + 1):
        zero_based = one_based - 1
        if zero_based < 0 or zero_based >= len(turns):
            continue
        turn = turns[zero_based]
        ratio, cached, new = _ratio_for_turn(turn)
        entry = {
            "turn": one_based,
            "cached_tokens": cached,
            "prefill_new_tokens": new,
            "ratio": ratio,
        }
        evaluated.append(entry)
        if ratio is None:
            skips.append(f"turn {one_based}: no token signal")
            continue
        if ratio < bound:
            failures.append(
                f"turn {one_based}: ratio={ratio:.3f} < bound={bound:.3f} "
                f"(cached={cached}, new={new})",
            )
    return evaluated, failures, skips


@scorer(metrics=[accuracy(), mean(), stderr()])
def hit_rate_per_turn(
    *,
    bound: float = 0.90,
    turn_range: list[int] | None = None,
):

    if not 0.0 <= bound <= 1.0:
        raise ValueError(
            f"hit_rate_per_turn: bound must be in [0.0, 1.0], got {bound}",
        )

    async def score(state: TaskState, target: Target) -> Score:
        turns = get_turn_attrs(state)
        if not turns:
            return Score(
                value=INCORRECT,
                answer="no_data",
                explanation=(
                    "hit_rate_per_turn: no turn_attrs in state.store. "
                    "Did the scenario run in Mode B?"
                ),
                metadata={"bound": bound, "turn_count": 0},
            )

        if turn_range and len(turn_range) >= 2:
            start_i, end_i = int(turn_range[0]), int(turn_range[1])
        else:
            start_i, end_i = 2, len(turns)

        evaluated, failures, skips = _evaluate_hit_rate_turns(
            turns, start_i, end_i, bound,
        )

        if not evaluated:
            return Score(
                value=INCORRECT,
                answer="no_evaluated_turns",
                explanation=(
                    f"hit_rate_per_turn: turn_range={turn_range} "
                    f"yielded no eligible turns "
                    f"(total turns={len(turns)})."
                ),
                metadata={"bound": bound, "turn_count": len(turns)},
            )
        if skips and not failures and all(e["ratio"] is None for e in evaluated):
            return Score(
                value=INCORRECT,
                answer="all_turns_no_signal",
                explanation=(
                    f"hit_rate_per_turn: every evaluated turn had no token signal. "
                    f"Skips: {skips}"
                ),
                metadata={"bound": bound, "evaluated": evaluated, "skips": skips},
            )

        passed = not failures
        if passed:
            explanation = (
                f"hit_rate_per_turn PASS: every evaluated turn ≥ {bound:.3f}. "
                f"Evaluated={len(evaluated)} skipped={len(skips)}."
            )
        else:
            explanation = "hit_rate_per_turn FAIL:\n" + "\n".join(failures)

        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=f"{len(evaluated) - len(failures)}/{len(evaluated)} ≥ {bound}",
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


__all__ = ["hit_rate_per_turn"]
