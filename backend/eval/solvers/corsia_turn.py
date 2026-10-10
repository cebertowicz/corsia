from __future__ import annotations

import os
from typing import Any

import httpx
from inspect_ai.solver import Generate, Solver, TaskState, solver

from eval.default_endpoint import DEFAULT_EVAL_ENDPOINT
from eval.credentials import AUTH_QUERY_KEY, resolve_eval_token


_DEFAULT_ENDPOINT = os.environ.get(
    "CORSIA_EVAL_ENDPOINT", DEFAULT_EVAL_ENDPOINT,
)
_DEFAULT_TIMEOUT = float(os.environ.get("CORSIA_EVAL_TIMEOUT", "180"))


@solver
def corsia_turn_solver(
    *,
    endpoint: str | None = None,
    token: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> Solver:
    ep = endpoint or _DEFAULT_ENDPOINT
    to = timeout or _DEFAULT_TIMEOUT
    extras = extra_metadata or {}

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        prompt = _extract_prompt(state)

        merged_meta: dict[str, Any] = {}
        if isinstance(state.metadata, dict):
            merged_meta.update(state.metadata)
        merged_meta.update(extras)
        if model is not None and "model" not in merged_meta:
            merged_meta["model"] = model
        if "timeout_seconds" not in merged_meta:
            merged_meta["timeout_seconds"] = to

        body = {"input": prompt, "metadata": merged_meta}
        # Outside the transport try: a missing credential is a setup error
        # that must stop the run, not a per-sample transport failure.
        auth = {AUTH_QUERY_KEY: resolve_eval_token(token)}

        async with httpx.AsyncClient(timeout=to) as client:
            try:
                resp = await client.post(
                    ep, params=auth, json=body,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                data = {
                    "content": None, "tool_calls": [], "iteration_count": 0,
                    "usage": {}, "trace_id": "0" * 32, "span_summary": {"spans": []},
                    "model": model or "", "ms_total": 0.0,
                    "error": f"HTTP {e.response.status_code}: {e.response.text[:500]}",
                }
            except Exception as e:
                data = {
                    "content": None, "tool_calls": [], "iteration_count": 0,
                    "usage": {}, "trace_id": "0" * 32, "span_summary": {"spans": []},
                    "model": model or "", "ms_total": 0.0,
                    "error": f"transport error: {e!s}",
                }

        state.store.set("corsia_eval_response", data)

        state.output.completion = data.get("content") or ""

        return state

    return solve


@solver
def null_tool_solver(
    *,
    endpoint: str | None = None,
    token: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
) -> Solver:
    return corsia_turn_solver(
        endpoint=endpoint,
        token=token,
        model=model,
        timeout=timeout,
        extra_metadata={"tools_in_scope": ["null_probe"]},
    )


def _extract_prompt(state: TaskState) -> str:
    txt = getattr(state, "input_text", None)
    if isinstance(txt, str) and txt.strip():
        return txt
    inp = state.input
    if isinstance(inp, str) and inp.strip():
        return inp
    up = getattr(state, "user_prompt", None)
    if up is not None:
        text = getattr(up, "text", None)
        if isinstance(text, str) and text.strip():
            return text
    return str(inp or "")


__all__ = ["corsia_turn_solver", "null_tool_solver"]
