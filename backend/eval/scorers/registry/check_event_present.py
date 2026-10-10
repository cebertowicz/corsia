from __future__ import annotations

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState

from ._events import get_events


@scorer(metrics=[accuracy(), mean(), stderr()])
def check_event_present(
    *,
    event_kind: str,
    min_count: int = 1,
    max_count: int | None = None,
):

    if not event_kind:
        raise ValueError("check_event_present: event_kind is required")
    if min_count < 0:
        raise ValueError("check_event_present: min_count must be ≥ 0")
    if max_count is not None and max_count < min_count:
        raise ValueError("check_event_present: max_count must be ≥ min_count")

    async def score(state: TaskState, target: Target) -> Score:
        events = get_events(state)
        matched_idx = [
            i for i, ev in enumerate(events)
            if ev.get("kind") == event_kind
        ]
        count = len(matched_idx)

        within_min = count >= min_count
        within_max = max_count is None or count <= max_count
        passed = within_min and within_max

        explanation = (
            f"event_kind={event_kind!r} count={count} "
            f"min={min_count} max={max_count} "
            f"({'PASS' if passed else 'FAIL'}); "
            f"total_events={len(events)}"
        )

        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=f"{count}",
            explanation=explanation,
            metadata={
                "event_kind": event_kind,
                "count": count,
                "min_count": min_count,
                "max_count": max_count,
                "matched_indexes": matched_idx,
                "total_events": len(events),
            },
        )

    return score


__all__ = ["check_event_present"]
