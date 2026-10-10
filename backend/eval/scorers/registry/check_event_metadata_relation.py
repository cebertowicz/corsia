from __future__ import annotations

import operator
from typing import Any, Callable

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState

from ._events import get_events


_RELATIONS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}


def _resolve_relation(event_kind: str, field: str, op: str) -> Callable[[Any, Any], bool]:
    if not event_kind:
        raise ValueError("check_event_metadata_relation: event_kind is required")
    if not field:
        raise ValueError("check_event_metadata_relation: field is required")
    if op not in _RELATIONS:
        raise ValueError(
            f"check_event_metadata_relation: op {op!r} not in "
            f"{sorted(_RELATIONS)}"
        )
    return _RELATIONS[op]


def _no_match_score(
    event_kind: str, field: str, op: str, value: Any, total_events: int,
) -> Score:
    return Score(
        value=INCORRECT,
        answer="no_match",
        explanation=(
            f"No events of kind={event_kind!r} found "
            f"(total_events={total_events}); cannot verify relation"
        ),
        metadata={
            "event_kind": event_kind,
            "field": field,
            "op": op,
            "value": value,
            "matched_count": 0,
            "violation_count": 0,
            "missing_field_count": 0,
            "total_events": total_events,
        },
    )


@scorer(metrics=[accuracy(), mean(), stderr()])
def check_event_metadata_relation(
    *,
    event_kind: str,
    field: str,
    op: str,
    value: Any,
):
    rel = _resolve_relation(event_kind, field, op)

    async def score(state: TaskState, target: Target) -> Score:
        events = get_events(state)
        matched = [ev for ev in events if ev.get("kind") == event_kind]

        if not matched:
            return _no_match_score(event_kind, field, op, value, len(events))

        violations: list[dict[str, Any]] = []
        missing_field: list[int] = []
        for idx, ev in enumerate(matched):
            payload = ev.get("payload") or {}
            if field not in payload:
                missing_field.append(idx)
                continue
            observed = payload[field]
            try:
                if not rel(observed, value):
                    violations.append({
                        "index": idx,
                        "observed": observed,
                        "iteration": ev.get("iteration"),
                    })
            except TypeError as e:
                violations.append({
                    "index": idx,
                    "observed": observed,
                    "iteration": ev.get("iteration"),
                    "type_error": str(e),
                })

        passed = not violations and not missing_field
        explanation = (
            f"event_kind={event_kind!r} field={field!r} op={op} "
            f"value={value!r} matched={len(matched)} "
            f"violations={len(violations)} missing_field={len(missing_field)} "
            f"({'PASS' if passed else 'FAIL'})"
        )
        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=f"{len(violations)+len(missing_field)} bad / {len(matched)}",
            explanation=explanation,
            metadata={
                "event_kind": event_kind,
                "field": field,
                "op": op,
                "value": value,
                "matched_count": len(matched),
                "violation_count": len(violations),
                "missing_field_count": len(missing_field),
                "violations": violations,
                "total_events": len(events),
            },
        )

    return score


__all__ = ["check_event_metadata_relation"]
