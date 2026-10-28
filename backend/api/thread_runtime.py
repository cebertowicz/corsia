"""Session runtime endpoints for active-turn and context snapshots."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from infra.auth import authenticate
from core.context_preview import build_next_turn_context_preview
from infra.execution_class import execution_class
from infra.context_preview_timing import ContextPreviewTiming
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from infra.send_dispatch import provider_for
from llm.resolution import (
    ModelUnavailableError,
    resolve_execution_target,
)
from core.turn_core import TurnIdentity
from core.turn_core import active_turns_for_owner
from api.thread_cache_warm import router as cache_warm_router
from api.checkpoints import router as checkpoints_router
from api.open_time import router as open_time_router
from api.thread_seen import router as thread_seen_router
from api.thread_update_cards import router as thread_update_cards_router

router = APIRouter(prefix="/api", tags=["threads"])
router.include_router(cache_warm_router)
router.include_router(checkpoints_router)
router.include_router(open_time_router)
router.include_router(thread_seen_router)
router.include_router(thread_update_cards_router)


def _load_next_turn_context_snapshot_preview(
    thread_id: str,
    user_id: str,
    model_override: str | None = None,
    tool_definitions: tuple[dict, ...] = (),
    expected_provider: str | None = None,
    runtime_truth: dict | None = None,
    timing=None,
):
    db = get_db()
    try:
        thread_row = get_thread_for_user(thread_id, user_id, db=db)
        if not thread_row:
            return None
        return build_next_turn_context_preview(
            thread_id,
            thread_row,
            db=db,
            current_user_content="",
            model_override=model_override,
            tool_definitions=tool_definitions,
            expected_provider=expected_provider,
            runtime_truth=runtime_truth,
            timing=timing,
        )
    finally:
        db.close()


def _load_next_turn_context_snapshot_provider(
    thread_id: str,
    user_id: str,
    model_override: str | None = None,
):
    """Resolve selected next-turn runtime facts before any Native tool startup."""
    db = get_db()
    try:
        thread_row = get_thread_for_user(thread_id, user_id, db=db)
        if not thread_row:
            return None
        if model_override:
            if hasattr(thread_row, "keys"):
                selected_thread_row = {
                    key: thread_row[key] for key in thread_row.keys()
                }
            elif isinstance(thread_row, dict):
                selected_thread_row = dict(thread_row)
            else:
                selected_thread_row = dict(thread_row)
            selected_thread_row["model"] = model_override
        else:
            selected_thread_row = thread_row
        selected_model = selected_thread_row["model"] or ""
        target = resolve_execution_target(selected_model, db=db)
        klass = execution_class(selected_thread_row, target=target)
        provider, _is_subagent = provider_for(klass)
        return provider, selected_thread_row, target
    finally:
        db.close()


@router.get("/threads/{thread_id}/active-turn-state")
async def active_turn_state(thread_id: str, token: str = ""):
    """Return a snapshot of the in-flight turn buffer for this session.

    WS ``subscribe_session`` is the primary rehydration path; this is fallback.
    """
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    db = get_db()
    try:
        thread_row = get_thread_for_user(thread_id, user["id"], db=db)
    finally:
        db.close()
    if not thread_row:
        raise HTTPException(404)

    from infra.state import turn_buffers as state_turn_buffers
    active = active_turns_for_owner(user["id"], thread_id=thread_id)
    identity = (
        TurnIdentity(active[0]["turn_id"], active[0]["turn_order"])
        if active else None
    )
    internal = (
        state_turn_buffers.snapshot(thread_id, identity)
        if identity is not None else None
    )
    snapshot = (
        {
            key: internal[key]
            for key in (
                "thread_id", "reasoning", "content", "active_tools",
                "started_at", "seq",
            )
        }
        if internal is not None else None
    )
    return {"active": snapshot is not None, "state": snapshot}


@router.get("/threads/{thread_id}/context-snapshot/next-turn")
async def next_turn_context_snapshot(
    thread_id: str,
    token: str = "",
    model: str | None = None,
):
    """Return the selected-harness CTX projection for the next dispatch."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    timing = ContextPreviewTiming(thread_id)
    try:
        provider_snapshot = await run_in_db(
            timing.wrap("provider_snapshot", _load_next_turn_context_snapshot_provider),
            thread_id,
            user["id"],
            model,
        )
    except (ModelUnavailableError, ValueError) as exc:
        # The sibling POST /warm handler already maps these to a structured
        # 409; an unavailable model must not 500 a read-only preview either.
        raise HTTPException(409, str(exc)) from exc
    if provider_snapshot is None:
        raise HTTPException(404)
    provider, selected_thread_row, target = provider_snapshot
    from infra.runtime_truth import read_runtime_truth

    runtime_truth = await read_runtime_truth(
        thread_id, selected_thread_row, target=target, timing=timing,
    )

    tool_definitions: tuple[dict, ...] = ()
    if provider == "native":
        from core.agent import get_agent
        from core.native_context_prefix import _freeze_tool_definitions
        from tools.catalog import advertised_tool_definitions

        agent = await get_agent()
        await agent._connect_mcp()
        tool_definitions = _freeze_tool_definitions(
            advertised_tool_definitions(agent.tools)
        )
    elif provider in {"cc", "codex"}:
        # Same catalog-filtered ledger Native uses, built from a throwaway
        # registry so this never starts or touches the Native singleton/MCP
        # connection (#1952, #806): CC and Codex are advertised exactly this
        # list too (tools/catalog.py::advertised_tool_definitions).
        from core.native_context_prefix import _freeze_tool_definitions
        from subagents.harness_tool_bridge import build_tool_registry
        from tools.catalog import advertised_tool_definitions

        tool_definitions = timing.measure(
            "tool_registry",
            lambda: _freeze_tool_definitions(
                advertised_tool_definitions(build_tool_registry())
            ),
        )
    try:
        preview_submitted_at = time.perf_counter()
        preview = await run_in_db(
            timing.wrap(
                "preview_job", _load_next_turn_context_snapshot_preview,
                submitted_at=preview_submitted_at,
            ),
            thread_id,
            user["id"],
            model,
            tool_definitions,
            provider,
            runtime_truth,
            timing,
        )
    except (ModelUnavailableError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    if preview is None:
        raise HTTPException(404)

    timing.emit_if_slow(preview)
    return {"preview": preview}
