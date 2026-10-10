from __future__ import annotations

import json
from typing import Any

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState


@scorer(metrics=[accuracy(), mean(), stderr()])
def structured_validity_scorer(*, mode: str = "json"):

    async def score(state: TaskState, target: Target) -> Score:
        if mode == "tool_call":
            return _score_tool_calls(state)
        return _score_json_output(state)

    return score


def _score_json_output(state: TaskState) -> Score:
    try:
        resp = state.store.get("corsia_eval_response")
        content = resp.get("content") if isinstance(resp, dict) else None
    except Exception:
        content = None

    if not isinstance(content, str) or not content.strip():
        return Score(
            value=INCORRECT,
            answer="empty",
            explanation="No content to validate.",
        )

    try:
        parsed = json.loads(content)
    except Exception as e:
        stripped = _strip_code_fences(content)
        try:
            parsed = json.loads(stripped)
        except Exception:
            return Score(
                value=INCORRECT,
                answer="invalid_json",
                explanation=f"JSON parse failed: {e!s}",
                metadata={"content_prefix": content[:200]},
            )

    return Score(
        value=CORRECT,
        answer="valid_json",
        explanation=f"Parsed valid JSON: type={type(parsed).__name__}",
        metadata={"parsed_type": type(parsed).__name__},
    )


def _score_tool_calls(state: TaskState) -> Score:
    try:
        resp = state.store.get("corsia_eval_response")
        tool_calls = resp.get("tool_calls") if isinstance(resp, dict) else []
    except Exception:
        tool_calls = []

    if not tool_calls:
        return Score(
            value=INCORRECT,
            answer="no_tool_calls",
            explanation="No tool calls captured.",
        )

    invalid: list[dict[str, Any]] = []
    for tc in tool_calls:
        args = tc.get("arguments")
        if isinstance(args, dict):
            continue  # already a dict
        if not isinstance(args, str):
            invalid.append({"id": tc.get("id"), "reason": "non-string"})
            continue
        try:
            json.loads(args)
        except Exception as e:
            invalid.append({"id": tc.get("id"), "reason": f"json_decode: {e!s}"})

    if invalid:
        return Score(
            value=INCORRECT,
            answer="invalid_args",
            explanation=f"{len(invalid)}/{len(tool_calls)} tool calls had unparseable arguments",
            metadata={"invalid": invalid},
        )

    return Score(
        value=CORRECT,
        answer="valid_args",
        explanation=f"All {len(tool_calls)} tool calls have parseable arguments",
        metadata={"tool_call_count": len(tool_calls)},
    )


def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if len(lines) >= 2:
            inner = "\n".join(lines[1:])
            if inner.endswith("```"):
                inner = inner[:-3]
            return inner.strip()
    return s


__all__ = ["structured_validity_scorer"]
