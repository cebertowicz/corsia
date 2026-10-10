from __future__ import annotations

from typing import Any

from inspect_ai.scorer import Score, Target, mean, scorer, stderr
from inspect_ai.solver import TaskState


@scorer(metrics=[mean(), stderr()])
def null_tool_paired_scorer():

    async def score(state: TaskState, target: Target) -> Score:
        try:
            resp = state.store.get("corsia_eval_response")
            ms_total = float(resp.get("ms_total")) if isinstance(resp, dict) else None
        except Exception:
            ms_total = None

        meta = state.metadata or {}
        pair_id = str(meta.get("pair_id") or "")
        mode = str(meta.get("mode") or "")

        if ms_total is None:
            return Score(
                value=0.0,
                answer="missing",
                explanation="ms_total missing from solver response",
                metadata={"pair_id": pair_id, "mode": mode},
            )

        return Score(
            value=ms_total,
            answer=f"{ms_total:.2f}ms",
            explanation=f"wall-clock={ms_total:.2f}ms mode={mode} pair_id={pair_id}",
            metadata={
                "pair_id": pair_id,
                "mode": mode,
                "ms_total": ms_total,
                "trace_id": (resp.get("trace_id") if isinstance(resp, dict) else "") or "",
            },
        )

    return score


def compute_pair_deltas(per_sample_scores: list[dict[str, Any]]) -> dict[str, float]:
    by_pair: dict[str, dict[str, float]] = {}
    for sc in per_sample_scores:
        meta = sc.get("metadata", {}) or {}
        pid = meta.get("pair_id")
        mode = meta.get("mode")
        ms_total = meta.get("ms_total")
        if not pid or ms_total is None:
            continue
        bucket = by_pair.setdefault(pid, {})
        if mode == "seam_paired_real":
            bucket["real"] = float(ms_total)
        elif mode == "seam_paired_null":
            bucket["null"] = float(ms_total)
    deltas: dict[str, float] = {}
    for pid, bucket in by_pair.items():
        if "real" in bucket and "null" in bucket:
            deltas[pid] = round(bucket["real"] - bucket["null"], 3)
    return deltas


__all__ = ["null_tool_paired_scorer", "compute_pair_deltas"]
