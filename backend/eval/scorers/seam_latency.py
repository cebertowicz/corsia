from __future__ import annotations

from typing import Any

from inspect_ai.scorer import (
    Score, Target, accuracy, mean, scorer, stderr, CORRECT, INCORRECT,
)
from inspect_ai.solver import TaskState


@scorer(metrics=[accuracy(), mean(), stderr()])
def seam_latency_scorer(*, budget_ms: float = 100.0):

    async def score(state: TaskState, target: Target) -> Score:
        spans = _extract_spans(state)
        if not spans:
            return Score(
                value=INCORRECT,
                explanation="No OTel spans found in solver output. Did the eval endpoint return span_summary?",
                metadata={"trace_id": _extract_trace_id(state)},
            )

        loop_spans = [s for s in spans if s.get("name") == "corsia.tool_loop"]
        tool_call_spans = [s for s in spans if s.get("name") == "corsia.tool_call"]

        if not loop_spans:
            return Score(
                value=CORRECT,
                answer="no_tool_loops",
                explanation="No tool_loop spans (not a tool-using turn).",
                metadata={
                    "trace_id": _extract_trace_id(state),
                    "loop_count": 0,
                    "tool_call_count": len(tool_call_spans),
                },
            )

        per_loop = []
        total_seam_ns = 0
        total_inference_ns = 0
        for loop in loop_spans:
            loop_start = loop["start_ns"]
            loop_end = loop["end_ns"]
            loop_dur_ns = loop_end - loop_start
            inside = [
                tc for tc in tool_call_spans
                if tc["start_ns"] >= loop_start and tc["end_ns"] <= loop_end
            ]
            tool_total_ns = sum((tc["end_ns"] - tc["start_ns"]) for tc in inside)
            seam_ns = loop_dur_ns - tool_total_ns
            per_loop.append({
                "loop_dur_ms": _ns_ms(loop_dur_ns),
                "tool_call_count": len(inside),
                "tool_total_ms": _ns_ms(tool_total_ns),
                "seam_ms": _ns_ms(seam_ns),
                "tool_names": [tc.get("attrs", {}).get("tool.name") for tc in inside],
            })
            total_seam_ns += max(seam_ns, 0)
            total_inference_ns += tool_total_ns

        worst_seam_ms = max((p["seam_ms"] for p in per_loop), default=0.0)
        passed = worst_seam_ms <= budget_ms

        explanation = (
            f"worst_seam_ms={worst_seam_ms:.2f} "
            f"vs budget_ms={budget_ms:.2f} "
            f"({'PASS' if passed else 'FAIL'}); "
            f"loops={len(loop_spans)} tool_calls={len(tool_call_spans)}"
        )

        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=f"{worst_seam_ms:.2f}ms",
            explanation=explanation,
            metadata={
                "trace_id": _extract_trace_id(state),
                "budget_ms": budget_ms,
                "worst_seam_ms": worst_seam_ms,
                "total_seam_ms": _ns_ms(total_seam_ns),
                "total_tool_ms": _ns_ms(total_inference_ns),
                "loop_count": len(loop_spans),
                "tool_call_count": len(tool_call_spans),
                "per_loop": per_loop,
            },
        )

    return score



def _extract_spans(state: TaskState) -> list[dict[str, Any]]:
    try:
        resp = state.store.get("corsia_eval_response")
        if not isinstance(resp, dict):
            return []
        summary = resp.get("span_summary") or {}
        spans = summary.get("spans")
        if not isinstance(spans, list):
            return []
        return spans
    except (AttributeError, KeyError):
        return []


def _extract_trace_id(state: TaskState) -> str:
    try:
        resp = state.store.get("corsia_eval_response")
        if isinstance(resp, dict):
            return str(resp.get("trace_id") or "")
    except (AttributeError, KeyError):
        pass
    return ""


def _ns_ms(ns: int) -> float:
    return round(ns / 1_000_000.0, 3)


__all__ = ["seam_latency_scorer"]
