from __future__ import annotations

import json
import os
import re
from typing import Any, Awaitable, Callable

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr,
)
from inspect_ai.solver import TaskState

from ._events import get_turn_attrs


def _default_judge_model() -> str:
    judge = os.environ.get("CORSIA_WORKFLOW_JUDGE_MODEL")
    if judge and judge.strip():
        return judge.strip()
    raise RuntimeError(
        "No workflow-quality judge model configured. Set "
        "CORSIA_WORKFLOW_JUDGE_MODEL (the GHA gate sets this per PR-label "
        "routing). No hardcoded default model is used."
    )


JudgeFn = Callable[[str, str], Awaitable[str]]


def _resolve_judge_model_id(model: str | None, judge_call: JudgeFn) -> str | None:
    if model:
        return model
    if judge_call is _default_judge_call:
        return _default_judge_model()
    return None


def _extract_eval_state(state: TaskState) -> tuple[dict, list[dict]]:
    response = state.store.get("corsia_eval_response") or {}
    if not isinstance(response, dict):
        response = {}
    return response, get_turn_attrs(state)


async def _default_judge_call(model_id: str, prompt: str) -> str:
    from inspect_ai.model import get_model

    model = get_model(model_id)
    output = await model.generate(prompt)
    return str(getattr(output, "completion", "") or "")



_JUDGE_PROMPT_TEMPLATE = """\
You are a strict evaluator scoring an AI assistant's workflow performance.

RUBRIC:
{rubric}

TRANSCRIPT:
{transcript}

INSTRUCTIONS:
- Read the rubric carefully. Each bullet is a criterion.
- Read the transcript: user messages, assistant messages, tool calls,
  tool results, and (when present) per-turn cache/latency attributes.
- Decide how well the run satisfies the rubric. Be unforgiving of
  partial completion, swallowed errors, and tool-call mistakes.
- Output ONLY a single line of JSON with two fields:
  {{"score": <float 0.0 to 1.0>, "reason": "<one sentence, <=200 chars>"}}

A score of 1.0 means every rubric criterion is fully satisfied. 0.0 means
the run is wholly broken. 0.7 is the default pass bar; below 0.5 is a
clear regression.

Respond with ONLY the JSON. No prose before or after.
"""


def _serialize_transcript(response: dict[str, Any], turn_attrs: list[dict[str, Any]]) -> str:
    out_lines: list[str] = []

    history = response.get("history")
    if isinstance(history, list) and history:
        for i, msg in enumerate(history):
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "?"))
            content = msg.get("content")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        if p.get("type") == "text":
                            parts.append(str(p.get("text", "")))
                        elif p.get("type") == "tool_use":
                            parts.append(
                                f"[tool_use {p.get('name')} args={json.dumps(p.get('input') or {})[:200]}]"
                            )
                        elif p.get("type") == "tool_result":
                            parts.append(
                                f"[tool_result {p.get('tool_use_id', '?')}: {str(p.get('content', ''))[:200]}]"
                            )
                        else:
                            parts.append(json.dumps(p)[:200])
                    else:
                        parts.append(str(p)[:200])
                content_text = " ".join(parts)
            else:
                content_text = str(content) if content is not None else ""
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                tc_summary = "; ".join(
                    f"{tc.get('name', '?')}({json.dumps(tc.get('arguments', {}))[:120]})"
                    for tc in tool_calls
                    if isinstance(tc, dict)
                )
                content_text = (content_text + "  TOOL_CALLS=" + tc_summary).strip()
            out_lines.append(f"[{i}] {role}: {content_text[:1500]}")
    else:
        if response.get("content"):
            out_lines.append(f"assistant_final: {str(response['content'])[:1500]}")
        tcs = response.get("tool_calls") or []
        if isinstance(tcs, list) and tcs:
            for tc in tcs:
                if isinstance(tc, dict):
                    out_lines.append(
                        f"tool_call: {tc.get('name')}({json.dumps(tc.get('arguments', {}))[:200]})"
                    )

    if response.get("error"):
        out_lines.append(f"ENDPOINT_ERROR: {response['error']!s}")

    if turn_attrs:
        keep_keys = (
            "turn_index", "cached_tokens", "prefill_new_tokens",
            "prompt_tokens", "completion_tokens", "ttft_ms",
            "iteration_count", "error",
        )
        compact = [
            {k: t.get(k) for k in keep_keys if k in t}
            for t in turn_attrs
        ]
        out_lines.append("PER_TURN_ATTRS: " + json.dumps(compact))

    return "\n".join(out_lines) if out_lines else "(empty transcript)"



_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_judge_response(raw: str) -> tuple[float | None, str]:
    if not raw or not raw.strip():
        return None, "judge returned empty response"

    candidates = _JSON_OBJECT_RE.findall(raw)
    if not candidates:
        candidates = [raw.strip()]

    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        score_raw = parsed.get("score")
        if isinstance(score_raw, bool):
            continue
        if not isinstance(score_raw, (int, float)):
            continue
        score = float(score_raw)
        if not 0.0 <= score <= 1.0:
            return None, f"judge score out of range [0,1]: {score}"
        reason = str(parsed.get("reason") or "")[:300]
        return score, reason

    return None, f"could not parse JSON from judge response: {raw[:200]!r}"



@scorer(metrics=[accuracy(), mean(), stderr()])
def workflow_quality(
    *,
    rubric: str,
    model: str | None = None,
    threshold: float = 0.7,
    _judge_fn: JudgeFn | None = None,
):
    if not rubric or not rubric.strip():
        raise ValueError("workflow_quality: rubric is required")
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(
            f"workflow_quality: threshold must be in [0,1], got {threshold}"
        )

    judge_call = _judge_fn or _default_judge_call

    async def score(state: TaskState, target: Target) -> Score:
        model_id = _resolve_judge_model_id(model, judge_call)
        response, turn_attrs = _extract_eval_state(state)

        transcript = _serialize_transcript(response, turn_attrs)
        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            rubric=rubric.strip(),
            transcript=transcript,
        )

        try:
            raw = await judge_call(model_id, prompt)
        except Exception as e:
            return Score(
                value="I",
                answer="judge_error",
                explanation=f"judge call raised: {e!s}",
                metadata={
                    "score": None,
                    "threshold": threshold,
                    "model": model_id,
                    "rubric_prefix": rubric.strip()[:200],
                },
            )

        score_val, reason = _parse_judge_response(raw)
        if score_val is None:
            return Score(
                value="I",
                answer="parse_error",
                explanation=f"workflow_quality: {reason}",
                metadata={
                    "score": None,
                    "threshold": threshold,
                    "model": model_id,
                    "raw_response": raw[:500],
                },
            )

        passed = score_val >= threshold
        return Score(
            value="C" if passed else "I",
            answer=f"{score_val:.2f}",
            explanation=(
                f"workflow_quality {'PASS' if passed else 'FAIL'}: "
                f"score={score_val:.2f} threshold={threshold:.2f} — {reason}"
            ),
            metadata={
                "score": score_val,
                "threshold": threshold,
                "model": model_id,
                "reason": reason,
                "transcript_len": len(transcript),
                "turn_count": len(turn_attrs),
            },
        )

    return score


__all__ = [
    "workflow_quality",
    "_default_judge_model",
    "_parse_judge_response",
    "_serialize_transcript",
]
