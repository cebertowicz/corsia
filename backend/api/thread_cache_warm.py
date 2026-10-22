"""Exact Native/oMLX browser-warm endpoint."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from infra.auth import authenticate
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from llm.context_window_discovery import ensure_context_window
from llm.resolution import (
    ExecutionTarget,
    ModelUnavailableError,
    resolve_thread_target,
)

router = APIRouter()


def _is_warmable_omlx_target(target: ExecutionTarget) -> bool:
    return bool(
        target.available
        and target.harness == "native"
        and target.provider == "omlx"
        and target.backend == "network"
    )


def _has_positive_context_evidence(target: ExecutionTarget) -> bool:
    return target.context_window is not None and target.context_window > 0


def _warm_suppressed(suppression: str) -> dict:
    return {
        "ok": False, "skipped": True, "reason": suppression,
        "message": "Warm suppressed while the session has active delivery work",
    }


def _load_warm_target(thread_id: str, user_id: str, model: str | None = None):
    """Resolve what to warm: the thread's model, or ``model`` while it has none.

    Nothing here binds a model; picking stays the person's step. A ``model``
    that disagrees with the thread's own is refused rather than ignored.
    """
    from core.thread_warm import warm_suppression_reason
    from llm.reasoning_config import effective_reasoning_effort

    db = get_db()
    try:
        sess = get_thread_for_user(thread_id, user_id, db=db)
        if not sess:
            raise HTTPException(404)
        suppression = warm_suppression_reason(thread_id, db=db)
        if suppression is not None:
            return _warm_suppressed(suppression)
        bound = sess["model"] or ""
        if bound and model and model != bound:
            raise HTTPException(409, "Thread is bound to a different model")
        try:
            target = resolve_thread_target(bound or model or "", thread_id=thread_id, db=db)
        except (ModelUnavailableError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}
        effort = effective_reasoning_effort(
            sess["reasoning_effort"] if "reasoning_effort" in sess.keys() else None,
            target.catalog_id,
        )
        if effort and not target.supports_reasoning:
            effort = None
        return target, sess["project_id"], effort
    finally:
        db.close()


async def _prove_warm_target(target: ExecutionTarget, thread_id: str):
    if not _is_warmable_omlx_target(target):
        return {"ok": False, "message": "Not a warmable available Native oMLX network model"}
    if _has_positive_context_evidence(target):
        return target
    await ensure_context_window(target.catalog_id)
    db = get_db()
    try:
        try:
            target = resolve_thread_target(target.catalog_id, thread_id=thread_id, db=db)
        except (ModelUnavailableError, ValueError) as exc:
            return {"ok": False, "message": str(exc)}
    finally:
        db.close()
    if not _has_positive_context_evidence(target):
        return {"ok": False, "message": f"Exact context-window evidence is unknown for {target.catalog_id}"}
    if not _is_warmable_omlx_target(target):
        return {"ok": False, "message": "Not a warmable available Native oMLX network model"}
    return target


async def _build_warm_prefix(
    thread_id: str,
    project_id: str | None,
    target: ExecutionTarget,
    reasoning_effort: str | None,
):
    from core.agent import get_agent
    from core.native_context_prefix import _freeze_tool_definitions
    from llm.base import sanitize_empty_content, sanitize_messages
    from llm.providers.omlx import make_prompt_extra_body
    from llm.reasoning_config import reasoning_config_for_model
    from tools.catalog import advertised_tool_definitions

    agent = await get_agent()
    await agent._connect_mcp()
    tool_defs = _freeze_tool_definitions(advertised_tool_definitions(agent.tools))
    wire_tool_defs = list(tool_defs)
    candidates, eager_checkpoint_id = await run_in_db(
        _warm_replay_candidates, thread_id, target.catalog_id,
    )
    messages, _prefix_info = await _render_warm_replay(
        agent=agent,
        thread_id=thread_id,
        project_id=project_id,
        target=target,
        candidates=candidates,
        eager_checkpoint_id=eager_checkpoint_id,
        tool_defs=tool_defs,
    )
    messages = sanitize_messages(sanitize_empty_content(messages))
    prompt_shape = make_prompt_extra_body(
        messages,
        reasoning_effort,
        preserve_thinking=target.preserve_thinking_default,
        reasoning_config=reasoning_config_for_model(target.catalog_id),
    )
    return messages, wire_tool_defs, prompt_shape


async def _render_warm_replay(
    *, agent, thread_id: str, project_id: str | None, target,
    candidates, eager_checkpoint_id: str | None, tool_defs,
):
    from core.destination_preflight import attach_native_provider_fit_ledger
    from core.native_context_prefix import build_real_send_prefix

    checkpoint_tail = candidates.checkpoint_tail
    selections = (
        (checkpoint_tail,)
        if checkpoint_tail is not None
        else (candidates.exact,)
    )
    block_reason = None
    for selection in selections:
        if selection is None:
            continue
        messages, prefix_info = await build_real_send_prefix(
            agent=agent,
            thread_id=thread_id,
            turn_model=target.transport_model,
            fresh_context=False,
            project_id=project_id,
            model_context_window=target.context_window,
            replay_selection=selection,
            tool_definitions=tool_defs,
            selected_model=target.catalog_id,
        )
        ledger = attach_native_provider_fit_ledger(
            model=target.transport_model,
            target_catalog_id=target.catalog_id,
            proven_target_window_tokens=target.context_window,
            messages=messages,
            tools=list(tool_defs),
            input_identity=f"warm:{thread_id}",
            current_input=None,
        ).fit_ledger
        if ledger.fits:
            from core.installed_context import persist_installed_delivery

            await run_in_db(
                persist_installed_delivery, prefix_info.get("installed_context"),
            )
            return messages, prefix_info
        block_reason = ledger.block_reason
    if block_reason is not None:
        raise ValueError(block_reason)
    raise ValueError("Native cache warm has no complete replay selection")


def _warm_replay_candidates(thread_id: str, target_catalog_id: str):
    """Freeze the two legal replay sources a live Native send may consume."""
    from core.context_replay import read_replay_candidates
    from infra.runtime_truth import provider_boundary_snapshot

    db = get_db()
    try:
        db.execute("BEGIN")
        candidates = read_replay_candidates(thread_id, db=db)
        if candidates.exact is None and candidates.checkpoint_tail is None:
            raise ValueError("Native cache warm has no complete replay selection")
        _, _, eager = provider_boundary_snapshot(
            thread_id, "native", target_catalog_id, db=db,
        )
        return candidates, eager.id if eager is not None else None
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.close()


def _warm_transport_identity(target: ExecutionTarget) -> tuple[str, str] | None:
    from llm.local_backends import OmlxBackend, find_backend

    backend = find_backend(target.transport_model)
    if not isinstance(backend, OmlxBackend):
        return None
    server_id, model_id = backend._parse(target.transport_model)
    if not server_id or not model_id:
        return None
    return server_id, model_id


def _probe_progress(probe: dict) -> dict:
    return {
        "total_blocks": probe.get("total_blocks"),
        "cold_blocks_at_start": probe.get("blocks_cold", 0),
        "block_size": probe.get("block_size", 0),
        "total_tokens": probe.get("total_tokens", 0),
        "ready_blocks": probe.get("blocks_ssd_hot"),
    }


def _probe_is_memory_hot(probe: dict) -> bool:
    total = int(probe.get("total_blocks") or 0)
    block_size = int(probe.get("block_size") or 0)
    total_tokens = int(probe.get("total_tokens") or 0)
    complete_blocks = min(total, total_tokens // block_size) if block_size > 0 else 0
    partial_blocks = total - complete_blocks
    return bool(
        probe.get("ok")
        and complete_blocks > 0
        and int(probe.get("blocks_ssd_hot") or 0) >= complete_blocks
        and int(probe.get("blocks_ssd_disk") or 0) == 0
        and int(probe.get("blocks_cold") or 0) <= partial_blocks
    )


async def _submit_warm(
    thread_id: str,
    target: ExecutionTarget,
    messages: list[dict],
    wire_tool_defs: list[dict],
    prompt_shape: dict,
    wait: bool = False,
    user_id: str | None = None,
    requested_model: str | None = None,
):
    from llm.providers import for_provider

    identity = _warm_transport_identity(target)
    if identity is None:
        return {
            "ok": False,
            "message": f"unparseable model string: {target.transport_model}",
        }
    server_id, model_id = identity
    provider = for_provider(target.provider)
    cfg = {"server_id": server_id}
    probe = await provider.probe_cache(
        cfg,
        model_id=model_id,
        messages=messages,
        tools=wire_tool_defs,
        **prompt_shape,
    )
    if probe.get("ok") and probe.get("model_loaded") is False:
        return {"ok": False, "message": f"model not loaded: {model_id}"}
    progress = _probe_progress(probe) if probe.get("ok") else None
    # Progress is emitted only while a submitted warm is running; the setup UI
    # never polls this endpoint, so there is no separate probe-only contract.
    if _probe_is_memory_hot(probe):
        return {
            "ok": True,
            "message": "Already warm",
            "progress": progress,
            "skipped": True,
        }
    return await _start_warm_owner(
        thread_id,
        target.catalog_id,
        provider,
        cfg,
        model_id,
        messages,
        wire_tool_defs,
        prompt_shape,
        progress,
        wait,
        user_id=user_id,
        event_model=requested_model or target.catalog_id,
    )


async def _start_warm_owner(
    thread_id: str,
    catalog_id: str,
    provider,
    cfg: dict,
    model_id: str,
    messages: list[dict],
    tools: list[dict],
    prompt_shape: dict,
    progress: dict | None,
    wait: bool = False,
    *,
    user_id: str | None,
    event_model: str,
):
    from core.thread_warm import (
        await_warm_outcome,
        make_provider_warm_operations,
        make_user_warm_progress_callback,
        start_thread_warm,
        warm_prefix_key,
        warm_suppression_reason,
    )

    def _admission_check() -> str | None:
        db = get_db()
        try:
            return warm_suppression_reason(thread_id, db=db)
        finally:
            db.close()

    suppression = _admission_check()
    if suppression is not None:
        return _warm_suppressed(suppression)
    key = warm_prefix_key(thread_id, model_id, messages, tools, prompt_shape)

    warm, progress_probe = make_provider_warm_operations(
        provider, cfg, model_id, messages, tools, prompt_shape, _probe_progress,
    )
    progress_callback = make_user_warm_progress_callback(
        user_id, thread_id, event_model,
    )
    task, started, suppression = await start_thread_warm(
        thread_id,
        key,
        warm,
        model=catalog_id,
        admission_check=_admission_check,
        progress_probe=progress_probe,
        progress_callback=progress_callback,
        initial_progress=progress,
    )
    if suppression is not None:
        return _warm_suppressed(suppression)
    if wait and task is not None:
        # Started here or joined in flight: answer with how the warm ended.
        return await await_warm_outcome(task)
    if not started:
        return {
            "ok": True,
            "skipped": True,
            "message": "Identical warm already in progress",
        }

    response = {"ok": True, "message": "Warm request submitted"}
    if progress is not None:
        response["progress"] = progress
    return response


@router.post("/threads/{thread_id}/warm")
async def warm_session_cache(
    thread_id: str, token: str = "", model: str | None = None, wait: bool = False,
):
    """Pre-warm the exact Native oMLX prefix; ``wait`` reports how it ended."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)
    loaded = _load_warm_target(thread_id, user["id"], model)
    if isinstance(loaded, dict):
        return loaded
    target, project_id, reasoning_effort = loaded
    proved = await _prove_warm_target(target, thread_id)
    if isinstance(proved, dict):
        return proved
    messages, tools, prompt_shape = await _build_warm_prefix(
        thread_id,
        project_id,
        proved,
        reasoning_effort,
    )
    return await _submit_warm(
        thread_id,
        proved,
        messages,
        tools,
        prompt_shape,
        wait,
        user_id=user["id"],
        requested_model=model,
    )
