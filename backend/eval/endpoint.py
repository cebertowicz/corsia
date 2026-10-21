from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from infra.auth import authenticate
from eval.fresh_turn import run_fresh_turn
from eval.otel import (
    setup_tracing, get_recorder, current_trace_id_hex, env_says_eval_mode,
)
from eval.null_tool import NullProbeTool, NULL_TOOL_NAME

router = APIRouter(prefix="/api", tags=["eval"])


class EvalTurnRequest(BaseModel):

    input: str = Field(..., description="User prompt.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Forwarded as eval-context. Reserved keys: model, "
                    "tools_in_scope, params (temperature/max_tokens overrides), "
                    "seed, scenario, mode, timeout_seconds.",
    )


class EvalTurnResponse(BaseModel):
    content: str | None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    iteration_count: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    trace_id: str
    span_summary: dict[str, Any] = Field(default_factory=dict)
    model: str
    ms_total: float
    error: str | None = None


async def _build_eval_agent(
    *,
    model_override: str | None,
    tools_in_scope: list[str] | None,
):
    from core.agent import get_agent
    agent = await get_agent()

    added_tools: list[str] = []
    if tools_in_scope and NULL_TOOL_NAME in tools_in_scope:
        if not agent.tools.has(NULL_TOOL_NAME):
            agent.tools.register(NullProbeTool())
            added_tools.append(NULL_TOOL_NAME)

    return agent, added_tools


def _required_model(metadata: dict[str, Any]) -> str:
    """Return the request's explicit model; an eval turn has no default."""
    explicit = metadata.get("model")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    raise HTTPException(
        status_code=422,
        detail="Eval turn requires `metadata.model`: an exact catalog model id.",
    )


@router.post("/eval/turn", response_model=EvalTurnResponse)
async def eval_turn(
    request: Request,
    body: EvalTurnRequest,
    response: Response,
    token: str = "",
):
    user = authenticate(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    setup_tracing()
    metadata = body.metadata or {}
    tools_in_scope = metadata.get("tools_in_scope") or []
    timeout_seconds = float(metadata.get("timeout_seconds", 120.0))
    model = _required_model(metadata)
    agent, added_tools = await _build_eval_agent(
        model_override=model, tools_in_scope=tools_in_scope,
    )

    t0 = time.perf_counter()
    from eval.otel import get_tracer
    tracer = get_tracer("corsia.eval.endpoint")
    trace_id_hex: str | None = None
    fresh = None
    try:
        with tracer.start_as_current_span("corsia.eval.request") as eval_span:
            fresh = await run_fresh_turn(
                user_id=user["id"], prompt=body.input, model=model, timeout_seconds=timeout_seconds,
            )
            eval_span.set_attribute("eval.session_id", fresh.thread_id)
            eval_span.set_attribute("eval.model", model)
            eval_span.set_attribute("eval.scenario", str(metadata.get("scenario") or ""))
            eval_span.set_attribute("eval.mode", str(metadata.get("mode") or ""))
            trace_id_hex = current_trace_id_hex()
    finally:
        for tname in added_tools:
            try:
                agent.tools.unregister(tname)
            except Exception:
                pass

    ms_total = (time.perf_counter() - t0) * 1000.0
    recorder = get_recorder()
    spans: list[dict[str, Any]] = []
    if recorder is not None and trace_id_hex:
        try:
            spans = recorder.pop(int(trace_id_hex, 16))
        except Exception:
            spans = []

    assert fresh is not None
    result = fresh.result
    payload = EvalTurnResponse(
        content=result.content if result else None,
        tool_calls=result.tool_calls if result else [],
        iteration_count=_count_llm_iterations(spans),
        usage=result.usage if result else {},
        trace_id=trace_id_hex or ("0" * 32),
        span_summary={"spans": spans, "trace_id": trace_id_hex or ""},
        model=model,
        ms_total=round(ms_total, 3),
        error=fresh.error or (result.error if result else None),
    )
    if trace_id_hex:
        response.headers["x-trace-id"] = trace_id_hex
    return payload


def _count_llm_iterations(spans: list[dict[str, Any]]) -> int:
    return sum(1 for span in spans if span.get("name") == "corsia.llm_call")


__all__ = ["router"]
