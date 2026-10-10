from __future__ import annotations

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState

from ._events import get_events


@scorer(metrics=[accuracy(), mean(), stderr()])
def check_event_iteration_window(
    *,
    event_kind: str,
    iteration_min: int,
    iteration_max: int,
):
    if not event_kind:
        raise ValueError("check_event_iteration_window: event_kind is required")
    if iteration_min < 0:
        raise ValueError(
            "check_event_iteration_window: iteration_min must be ≥ 0"
        )
    if iteration_max < iteration_min:
        raise ValueError(
            "check_event_iteration_window: iteration_max must be ≥ iteration_min"
        )

    async def score(state: TaskState, target: Target) -> Score:
        events = get_events(state)
        in_window: list[dict] = []
        out_of_window: list[dict] = []
        for ev in events:
            if ev.get("kind") != event_kind:
                continue
            iteration = ev.get("iteration", 0)
            if iteration_min <= iteration <= iteration_max:
                in_window.append(ev)
            else:
                out_of_window.append(ev)

        passed = bool(in_window)
        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=f"{len(in_window)} in window",
            explanation=(
                f"event_kind={event_kind!r} window=[{iteration_min},{iteration_max}] "
                f"in_window={len(in_window)} out_of_window={len(out_of_window)} "
                f"({'PASS' if passed else 'FAIL'})"
            ),
            metadata={
                "event_kind": event_kind,
                "iteration_min": iteration_min,
                "iteration_max": iteration_max,
                "in_window_count": len(in_window),
                "out_of_window_count": len(out_of_window),
                "in_window_iterations": [
                    ev.get("iteration") for ev in in_window
                ],
                "out_of_window_iterations": [
                    ev.get("iteration") for ev in out_of_window
                ],
                "total_events": len(events),
            },
        )

    return score


__all__ = ["check_event_iteration_window"]
