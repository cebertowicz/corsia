from __future__ import annotations

import asyncio
import json
import time
import uuid
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from infra.auth import authenticate
from infra.thread_input_types import FailedInputBatch
from eval.otel import (
    setup_tracing, get_recorder, current_trace_id_hex, env_says_eval_mode,
)
from eval.null_tool import NullProbeTool, NULL_TOOL_NAME
from eval.turn_result import EvalTurnResult, read_turn_result

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


def _new_eval_session_id() -> str:
    return f"eval-{uuid.uuid4().hex[:12]}"


async def _open_eval_thread(user_id: str, model: str) -> tuple[str, dict]:
    """Create the eval turn's thread row and return it as the send path's row."""
    from infra.db_async import run_in_db
    from infra.fresh_threads import create_fresh_thread

    thread_id = _new_eval_session_id()
    await run_in_db(partial(
        create_fresh_thread, user_id=user_id, thread_id=thread_id,
        title="Eval turn", model=model,
    ))
    return thread_id, await run_in_db(_read_thread_row, thread_id)


def _read_thread_row(thread_id: str) -> dict:
    """Read the freshly created eval thread as the canonical dispatch row."""
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise RuntimeError(f"Eval thread {thread_id} disappeared after creation")
    return dict(row)


def _required_model(metadata: dict[str, Any]) -> str:
    """Return the request's explicit model; an eval turn has no default."""
    explicit = metadata.get("model")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    raise HTTPException(
        status_code=422,
        detail="Eval turn requires `metadata.model`: an exact catalog model id.",
    )


# Eval admission is owned by infra.thread_driver.admit_input.


async def _run_eval_turn(
    *,
    thread_id: str,
    thread_row: dict,
    prompt: str,
    model: str,
    timeout_seconds: float,
) -> tuple[str | None, str | None]:
    """Run one canonical Native Turn for the eval route and await its settlement.

    The route admits its input the way every programmatic ingress does
    (``subagents.cc_spawn``, ``plans.plan_wake``, ``events.delivery``): one FIFO
    input plus its idle-Turn claim, then the canonical claimed-batch execution,
    which renders the payload, records the Turn's context manifest and hands the
    batch to the Native runtime. Nothing here persists a second time, and no
    step of the Turn is observed from outside — the route reads the Turn's own
    durable rows afterwards.

    Unlike ``infra.thread_driver.admit_input`` it does not pull the thread's
    passive context digest into the batch. An eval turn measures the scenario
    input it was handed: the digest (a wall-clock line plus every context change
    the thread has not seen) would otherwise be part of the measured provider
    request, and the perf scenarios record their prefill and cache numbers from
    exactly this path.
    """
    from core.turn_core import BOOT_EPOCH
    from infra.db_async import run_in_db
    from infra.send_types import InboxQueuedInput
    from infra.thread_driver import admit_input
    from infra.thread_execution import execute_claimed_batch
    from infra.turn_admission_driver import drive_turn_admission
    from infra.time import utc_now_iso
    from llm.resolution import resolve_execution_target

    try:
        target = await run_in_db(resolve_execution_target, model)
        admission = await run_in_db(
            admit_input,
            thread_id,
            role="user",
            content=prompt,
            intent="apply_now",
            message_id=str(uuid.uuid4()),
            turn_id=str(uuid.uuid4()),
            boot_epoch=BOOT_EPOCH,
            admitted_at=utc_now_iso(),
            metadata={"sender_kind": "eval"},
            delivery_options={"native_mode": "delegation", "attachments": []},
            materialize_passive_digest=False,
        )
    except Exception as exc:
        return None, f"Eval turn errored: {_failure_detail(exc)}"
    if admission.held_for_restart:
        return None, "Eval turn is held during restart"
    batch = admission.batch
    if isinstance(batch, FailedInputBatch):
        await drive_turn_admission(batch)
        return None, f"Eval turn errored: {batch.error_text}"
    if batch is None:
        return None, "Eval turn could not claim an idle Turn for its input"
    try:
        send_result = await execute_claimed_batch(
            batch,
            thread_row=thread_row,
            target=target,
            user={"id": str(thread_row.get("user_id") or "")},
        )
    except Exception as exc:
        return None, f"Eval turn errored: {_failure_detail(exc)}"
    if isinstance(send_result, InboxQueuedInput) or send_result.turn_task is None:
        return None, "Eval turn was not handed to a Native runtime"
    try:
        await asyncio.wait_for(send_result.turn_task, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return None, f"Eval turn timed out after {timeout_seconds}s"
    except Exception as exc:
        return None, f"Eval turn errored: {_failure_detail(exc)}"
    if not send_result.turn_id:
        return None, "Eval turn completed without a Turn identity"
    return send_result.turn_id, None


def _failure_detail(error: BaseException) -> str:
    """Prefer a rejection's caller-facing detail over its bare repr."""
    detail = getattr(error, "detail", None)
    if isinstance(detail, str) and detail:
        return detail
    return str(error)


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

    thread_id, thread_row = await _open_eval_thread(user["id"], model)

    t0 = time.perf_counter()

    from eval.otel import get_tracer
    tracer = get_tracer("corsia.eval.endpoint")
    trace_id_hex: str | None = None
    err: str | None = None
    result: EvalTurnResult | None = None

    try:
        with tracer.start_as_current_span("corsia.eval.request") as eval_span:
            eval_span.set_attribute("eval.session_id", thread_id)
            eval_span.set_attribute("eval.model", model)
            eval_span.set_attribute("eval.scenario", str(metadata.get("scenario") or ""))
            eval_span.set_attribute("eval.mode", str(metadata.get("mode") or ""))
            trace_id_hex = current_trace_id_hex()

            turn_id, err = await _run_eval_turn(
                thread_id=thread_id, thread_row=thread_row,
                prompt=body.input, model=model,
                timeout_seconds=timeout_seconds,
            )
            if turn_id is not None:
                result = await read_turn_result(thread_id, turn_id)
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
            trace_int = int(trace_id_hex, 16)
            spans = recorder.pop(trace_int)
        except Exception:
            spans = []

    payload = EvalTurnResponse(
        content=result.content if result else None,
        tool_calls=result.tool_calls if result else [],
        iteration_count=_count_llm_iterations(spans),
        usage=result.usage if result else {},
        trace_id=trace_id_hex or ("0" * 32),
        span_summary={"spans": spans, "trace_id": trace_id_hex or ""},
        model=model,
        ms_total=round(ms_total, 3),
        error=err or (result.error if result else None),
    )

    if trace_id_hex:
        response.headers["x-trace-id"] = trace_id_hex

    return payload


def _count_llm_iterations(spans: list[dict[str, Any]]) -> int:
    return sum(1 for s in spans if s.get("name") == "corsia.llm_call")


__all__ = ["router"]
