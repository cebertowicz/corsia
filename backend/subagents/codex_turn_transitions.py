"""Exact Codex handle transitions beneath one CORSIA Turn."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core.stop_causes import PlanLimitStop
from core.turn_core import TerminalSettlement, drive_turn_settlement, terminal_error_detail
from infra.db_async import run_in_db
from subagents.codex_runtime_terminal import CommittedTerminalFailure
from subagents.live_registry import acquire_stop_lock
from subagents.runtime_guard import join_task_through_cancellation

if TYPE_CHECKING:
    from core.context_payload import PreparedContextPayload
    from core.turn_core import TurnIdentity
    from subagents.codex_runtime_spawn import AppServerTurnSpec
    from subagents.codex_runtime_state import _LiveCodex

InputTerminalSettlement = Callable[[str, str | BaseException | None], Awaitable[None]]


async def configure_spec_app_server_input(
    session,
    live: _LiveCodex,
    spec: AppServerTurnSpec,
    *,
    exact_model: str | None,
    tool_registry,
    dynamic_tools,
    on_event,
    on_exit,
):
    """Configure transport input directly from one immutable spawn spec."""
    from subagents.codex_runtime_spawn import configure_app_server_input

    return await configure_app_server_input(
        session,
        live=live,
        text=spec.text,
        binary=spec.binary,
        codex_thread_id=spec.codex_thread_id,
        working_dir=spec.working_dir,
        exact_model=exact_model,
        effort=spec.effort,
        history_items=spec.history_items,
        dynamic_tools=dynamic_tools,
        tool_registry=tool_registry,
        thread_id=spec.thread_id,
        project_id=spec.project_id,
        on_event=on_event,
        on_exit=on_exit,
        media_projection=spec.media_projection,
        client_message_id=spec.client_message_id,
    )


def _require_provider_fit(spec: AppServerTurnSpec, prepared) -> None:
    if prepared is None:
        return
    from core.destination_fit import require_provider_call_fit

    rendered = prepared.rendered
    require_provider_call_fit(
        prepared.fit_ledger,
        harness="codex",
        target_catalog_id=str(spec.catalog_model_id or ""),
        accounting_payload=rendered.accounting_payload,
        input_identity=rendered.fit_input_identity or "",
        current_input=rendered.fit_current_input,
    )


async def maintain_initial_boundary(
    spec: AppServerTurnSpec,
    live: _LiveCodex,
) -> TurnIdentity | None:
    """Checkpoint only after the initial Codex attempt owns its exact Turn."""
    from core.fork_codex_context import CodexForkContext

    prepared = spec.prepared_context
    refresh = spec.refresh_prepared_context
    if prepared is None or refresh is None:
        _require_provider_fit(spec, prepared)
        if prepared is not None:
            live.session.tool_context = CodexForkContext(
                None, prepared.fit_ledger.rendered_input_tokens,
            )
        return None
    from subagents.harness_checkpoint_resume import (
        maintain_external_initial_boundary,
    )

    adopted = await maintain_external_initial_boundary(
        live, spec.thread_id, "codex", prepared, refresh,
    )
    if adopted is None:
        _require_provider_fit(spec, prepared)
        live.session.tool_context = CodexForkContext(
            None, prepared.fit_ledger.rendered_input_tokens,
        )
        return None
    _require_provider_fit(spec, adopted)
    return await resume_same_turn_after_checkpoint_locked(
        spec.thread_id,
        live,
        adopted,
        current_user_text=spec.current_user_text,
        mark_accepted=spec.mark_accepted,
        mark_ambiguous=spec.mark_ambiguous,
        settle_delivery=spec.settle_delivery,
        transfer_delivery_to_turn=getattr(spec, "transfer_delivery_to_turn", None),
        settle_delivery_terminal=getattr(spec, "settle_delivery_terminal", None),
        media_projection=spec.media_projection,
        held_lock=spec.held_lock,
    )


def require_open_initial_boundary(
    resumed: TurnIdentity | None,
    live: _LiveCodex,
) -> None:
    """Reject a replacement provider write already owned by exact Stop."""
    if resumed is None and live.checkpoint_wait_scope.stop_requested:
        raise asyncio.CancelledError("Codex Stop requested before provider write")


async def resume_same_turn_after_checkpoint_locked(
    thread_id: str,
    expected_live: _LiveCodex,
    prepared_context: PreparedContextPayload,
    *,
    current_user_text: str,
    mark_accepted=None,
    mark_ambiguous=None,
    settle_delivery=None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: InputTerminalSettlement | None = None,
    media_projection=None,
    held_lock: asyncio.Lock,
) -> TurnIdentity:
    """Replace one bound Codex handle and send the supplied continuation."""
    from llm.resolution import resolve_thread_target
    from subagents import codex_runtime as runtime

    identity = expected_live.corsia_turn
    session = expected_live.session
    if identity is None or session is None:
        raise RuntimeError("Codex checkpoint continuation has no active Turn")
    target = resolve_thread_target(expected_live.catalog_model_id or "", thread_id=thread_id)
    if target.harness != "codex" or not target.available:
        raise RuntimeError("Codex checkpoint target is unavailable")
    if runtime._LIVE.get(thread_id) is not expected_live:
        raise RuntimeError("Codex handle changed during checkpoint wait")
    if expected_live.corsia_turn != identity or expected_live.tool_step_ids:
        raise RuntimeError("Codex checkpoint boundary is not settled")
    if not await runtime._fence_exact_live(thread_id, expected_live):
        raise RuntimeError("Codex checkpoint handle could not be retired")
    await runtime._release_delivery_event_barrier(expected_live)
    runtime._release_exact_live(thread_id, expected_live)
    if runtime._CODEX_THREADS.get(thread_id) == expected_live.codex_thread_id:
        runtime._CODEX_THREADS.pop(thread_id, None)
    expected_live.corsia_turn = None
    try:
        resumed = await runtime._spawn_turn(
            thread_id, current_user_text,
            successor_turn_id=identity.turn_id,
            codex_thread_id=None,
            binary=getattr(session, "binary", None),
            model=target.transport_model,
            catalog_model_id=target.catalog_id,
            harness=target.harness,
            windows=target.windows,
            expected_reported_model=target.expected_reported_model,
            prepared_context=prepared_context,
            working_dir=getattr(session, "working_dir", None),
            project_id=getattr(session, "project_id", None),
            effort=getattr(session, "effort", None), broadcast=runtime._default_broadcast,
            mark_accepted=mark_accepted,
            mark_ambiguous=mark_ambiguous,
            settle_delivery=settle_delivery,
            transfer_delivery_to_turn=transfer_delivery_to_turn,
            settle_delivery_terminal=settle_delivery_terminal,
            continuing_wait_scope=expected_live.checkpoint_wait_scope,
            preadmitted_turn=identity,
            media_projection=media_projection,
            held_lock=held_lock,
        )
    except BaseException as error:
        stop_requested = expected_live.checkpoint_wait_scope.stop_requested
        if stop_requested or isinstance(error, CommittedTerminalFailure):
            raise
        outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
        if settle_delivery_terminal is not None:
            await settle_delivery_terminal(outcome, terminal_error_detail(error) or None)
        terminal = await runtime._end_owned_turn(
            thread_id, expected_live, identity, outcome, error=str(error),
            bcast=runtime._default_broadcast, source="codex:checkpoint-replacement",
            defer_drive=True,
        )
        assert isinstance(terminal, TerminalSettlement)
        raise CommittedTerminalFailure(terminal, error) from error
    if resumed != identity:
        raise RuntimeError("Codex checkpoint continuation changed Turn identity")
    await run_in_db(runtime._retire_registered_thread_artifacts, thread_id,
                    keep_thread_id=runtime._CODEX_THREADS.get(thread_id))
    return identity


async def resume_same_turn_after_checkpoint(
    thread_id: str,
    expected_live: _LiveCodex,
    prepared_context: PreparedContextPayload,
) -> TurnIdentity:
    """Replace one Codex attempt at a settled mid-Turn boundary."""
    from infra.checkpoint_receipts import resume_anchor_text
    from subagents import codex_runtime as runtime

    if prepared_context.replay is None:
        raise RuntimeError("Codex checkpoint resume is missing its prepared selection")
    anchor = resume_anchor_text(prepared_context.replay.tail.resume_presence_line)
    lock = runtime._LIVE.lock(thread_id)
    try:
        async with lock:
            return await resume_same_turn_after_checkpoint_locked(
                thread_id, expected_live, prepared_context,
                current_user_text=anchor, held_lock=lock,
            )
    except CommittedTerminalFailure as failure:
        if failure.settlement is not None:
            await drive_turn_settlement(failure.settlement)
        raise failure.publication_error


async def _request_stop_owner(
    thread_id: str,
    expected_turn_id: str,
    expected_live: _LiveCodex | None,
):
    from subagents import codex_runtime as runtime
    from subagents.codex_runtime_support import request_stop_checkpoint_wait

    current = runtime._exact_stop_live(
        thread_id, expected_turn_id, expected_live,
    )
    if current is None:
        return None
    request_stop_checkpoint_wait(current)
    owner = runtime._request_exact_stop(
        thread_id, expected_turn_id, expected_live,
    )
    if owner is None:
        return None
    live, _session, _wait_requests_idle = owner
    if not await runtime._terminate_turn_foreground_process(thread_id, live):
        return None
    return owner


async def _settle_stop_owner(
    thread_id: str,
    expected_turn_id: str,
    owner,
    broadcast,
    stop_error: PlanLimitStop | None = None,
) -> tuple[bool, TerminalSettlement | None]:
    from infra.db_async import run_in_db
    from subagents import codex_runtime as runtime

    live, session, _wait_requests_idle = owner
    exact_live = runtime._exact_stop_live(thread_id, expected_turn_id, live)
    if exact_live is not live or live.session is not session:
        return False, None
    if not await runtime._fence_exact_live(thread_id, live):
        return False, None
    await runtime._release_delivery_event_barrier(live)
    try:
        stopped, settlement = await runtime._finish_interrupted_if_still_owned(
            thread_id, live, "stop_requested",
            broadcast or runtime._default_broadcast,
            defer_drive=True, stop_error=stop_error,
        )
    except CommittedTerminalFailure:
        runtime._release_exact_live(thread_id, live)
        assert runtime._LIVE.get(thread_id) is not live, (
            "Codex terminal retained exact runtime"
        )
        raise
    if not stopped and runtime._LIVE.get(thread_id) is live:
        stopped = await run_in_db(
            runtime._turn_has_durable_terminal, expected_turn_id,
        )
    if stopped:
        runtime._release_exact_live(thread_id, live)
    return stopped, settlement


async def _stop_exact(
    thread_id: str,
    *,
    expected_turn_id: str | None = None,
    expected_live: _LiveCodex | None = None,
    broadcast=None,
    cause: PlanLimitStop | None = None,
) -> bool:
    """Stop one exact Codex Turn and its synchronous checkpoint wait."""
    from subagents import codex_runtime as runtime

    if not expected_turn_id:
        return False
    initial = runtime._LIVE.get(thread_id)
    if runtime._is_stale_stop_live(initial, expected_turn_id, expected_live):
        return False
    lock = runtime._LIVE.existing_lock(thread_id)
    owner = await _request_stop_owner(
        thread_id, expected_turn_id, expected_live,
    )
    if owner is None:
        current = runtime._LIVE.get(thread_id)
        if current is not None and runtime._exact_stop_live(
            thread_id, expected_turn_id,
        ) is None:
            return False
        if lock is None or not lock.locked():
            return False
    lock = lock or runtime._LIVE.lock(thread_id)
    publication_error: BaseException | None = None
    try:
        # Same bound as Claude Code: past it, do_stop ends the Turn durably and
        # its retirement pass abandons the stalled lock generation.
        if not await acquire_stop_lock(lock, thread_id=thread_id, runtime="codex_runtime"):
            return False
        try:
            current = runtime._exact_stop_live(thread_id, expected_turn_id)
            if current is None:
                return False
            if owner is None or owner[0] is not current:
                owner = await _request_stop_owner(
                    thread_id, expected_turn_id, current,
                )
                if owner is None:
                    return False
            stopped, settlement = await _settle_stop_owner(
                thread_id, expected_turn_id, owner, broadcast, cause,
            )
        finally:
            lock.release()
    except CommittedTerminalFailure as failure:
        assert runtime._LIVE.get(thread_id) is None, (
            "Codex terminal survived exact runtime retirement"
        )
        stopped, settlement = True, failure.settlement
        publication_error = failure.publication_error
    if settlement is not None:
        assert stopped, "Codex terminal committed before exact runtime retirement"
        await drive_turn_settlement(settlement)
    if publication_error is not None:
        raise publication_error
    return stopped


def abandon_stalled_stop(thread_id: str, expected_turn_id: str) -> object | None:
    """Detach the owner Stop could not retire by its deadline; return its session."""
    from subagents import codex_runtime as runtime
    from subagents.codex_runtime_state import schedule_live_writer_release

    live = runtime._LIVE.abandon(thread_id, expected_turn_id)
    if live is None:
        return None
    schedule_live_writer_release(thread_id)
    live.checkpoint_wait_scope.request_stop()
    if runtime._APP_SESSIONS.get(thread_id) is live.session:
        runtime._APP_SESSIONS.pop(thread_id, None)
    return live.session


async def stop(
    thread_id: str,
    *,
    expected_turn_id: str | None = None,
    expected_live: _LiveCodex | None = None,
    broadcast=None,
    cause: PlanLimitStop | None = None,
) -> bool:
    """Finish one exact Stop even if its caller disconnects or is cancelled."""
    task = asyncio.create_task(_stop_exact(
        thread_id,
        expected_turn_id=expected_turn_id,
        expected_live=expected_live,
        broadcast=broadcast,
        cause=cause,
    ))
    cancellation = await join_task_through_cancellation(task)
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result
