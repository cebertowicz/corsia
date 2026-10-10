from __future__ import annotations

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState

from ._events import get_response, get_events


_DEFAULT_ERROR_KINDS: frozenset[str] = frozenset({"error"})


@scorer(metrics=[accuracy(), mean(), stderr()])
def check_no_provider_error(
    *,
    allow_kinds: list[str] | None = None,
):
    allow_set = frozenset(allow_kinds or ())

    async def score(state: TaskState, target: Target) -> Score:
        resp = get_response(state)
        events = get_events(state)

        err = resp.get("error")
        has_response_error = isinstance(err, str) and bool(err.strip())

        offending_events: list[dict] = []
        for ev in events:
            kind = ev.get("kind")
            if kind in _DEFAULT_ERROR_KINDS and kind not in allow_set:
                offending_events.append(ev)

        passed = not has_response_error and not offending_events

        if passed:
            explanation = (
                "no response.error AND no provider-error events "
                f"(events={len(events)} allow_kinds={sorted(allow_set)})"
            )
        else:
            parts = []
            if has_response_error:
                parts.append(f"response.error={err!r}")
            if offending_events:
                parts.append(
                    f"{len(offending_events)} error event(s) of kinds="
                    f"{sorted({e.get('kind') for e in offending_events})}"
                )
            explanation = "FAIL: " + "; ".join(parts)

        return Score(
            value=CORRECT if passed else INCORRECT,
            answer="clean" if passed else "errored",
            explanation=explanation,
            metadata={
                "error": err,
                "has_response_error": has_response_error,
                "error_event_count": len(offending_events),
                "allow_kinds": sorted(allow_set),
                "total_events": len(events),
            },
        )

    return score


__all__ = ["check_no_provider_error"]
