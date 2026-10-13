"""Codex CLI/runtime bridge for CORSIA chat threads."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core import turn_core
from core.stop_causes import PlanLimitStop
from infra.db_async import run_in_db, run_in_db_unless_owned
from infra.database import get_db, write_transaction
from infra.queries import get_thread_project_id, insert_message
from llm.context_window import WindowFacts
from llm.resolution import ExecutionTarget, Harness
from subagents import completed_tool_terminal as _tool_terminal
from core.turn_core import (
    TerminalSettlement,
    TurnIdentity,
    TurnTerminalResult,
    bind_preadmitted_turn,
    commit_turn_terminal,
    persist_turn_start_bound,
    persist_turn_terminal,
    settle_turn_terminal,
)
from core.turn_activity import turn_activity_frame
from subagents.codex_runtime_errors import (
    CodexLiveTurnBusy,
    CodexPreparedContextStale,
    surface_exit_error,
)
from subagents.codex_runtime_events import record_terminal_marker
from subagents.codex_runtime_event_delivery import (
    arm_delivery_event_barrier as _arm_delivery_event_barrier,
    await_delivery_settlement as _await_delivery_settlement,
    discard_delivery_event_barrier as _discard_delivery_event_barrier,
    fail_delivery_event_barrier as _fail_delivery_event_barrier,
    release_delivery_event_barrier as _release_delivery_event_barrier,
    runtime_event_handler as _make_on_event,
)
from subagents.codex_runtime_terminal import (
    CommittedTerminalFailure,
    finish_interrupted_if_still_owned as _finish_interrupted_if_still_owned,
    preserve_committed_terminal,
    publish_claimed_terminal as _publish_claimed_terminal,
    turn_has_durable_terminal as _turn_has_durable_terminal,
)
from subagents.codex_destination import (
    refresh_prepared_codex_context_after_boundary as _refresh_prepared_codex_context_after_boundary,
    resolve_prepared_codex_context as _resolve_prepared_codex_context,
)
from subagents.codex_runtime_config import (
    CodexConnectionUnavailable,
    codex_binary as _codex_binary,
    codex_connection as _codex_connection,
    ensure_connection_available,
)
from subagents.codex_turn_transitions import (
    configure_spec_app_server_input as _configure_spec_app_server_input,
    maintain_initial_boundary as _maintain_codex_initial_boundary,
    require_open_initial_boundary as _require_open_codex_initial_boundary,
    resume_same_turn_after_checkpoint,
    stop,
)
from subagents.codex_runtime_state import (
    _APP_SESSIONS,
    _CODEX_THREADS,
    _DEAD,
    _LIVE,
    _PROVIDER_CONTEXT_UNTRUSTED,
    _LiveCodex,
    current_handle_fingerprint,
    dead_generation_ready_for_settlement,
    finalize_live,
    mark_provider_context_untrusted,
    retire_exited_generation as _retire_exited_generation,
)
from subagents.codex_runtime_spawn import (
    AppServerTurnSpec as _AppServerTurnSpec, checkpoint_replay_id as _checkpoint_replay_id,
    causal_app_server_request as _causal_app_server_request,
    capture_app_server_request as _capture_app_server_request,
    new_app_server_live as _new_app_server_live,
    remember_spawned_turn as _remember_spawned_app_server_turn,
    resolve_app_server_tools as _resolve_app_server_tools,
    reusable_app_session as _reusable_app_session,
    stamp_app_server_context as _stamp_app_server_context,
)
from subagents.codex_spawn_preflight import (
    resolve_spawn_payload as _resolve_spawn_payload,
    spawn_binary as _spawn_binary,
    spawn_runtime_epoch as _spawn_runtime_epoch,
    validate_transport_model as _validate_codex_transport_model,
)
from subagents.codex_runtime_support import (
    async_method as _async_method,
    exact_stop_live as _exact_stop_live,
    has_prior_messages,
    is_stale_stop_live as _is_stale_stop_live,
    quarantined_unknown_write as _quarantined_unknown_write,
    request_exact_stop as _request_exact_stop,
    restore_thread_id as _restore_thread_id,
    terminate_app_session as _terminate_app_session,
    working_dir_for_reconstruction as _working_dir_for_reconstruction,
)

if TYPE_CHECKING:
    from core.context_payload import PreparedContextPayload

_PreparedRefresh = Callable[[], Awaitable["PreparedContextPayload"]]
_InputTerminalSettlement = Callable[[str, str | BaseException | None], Awaitable[None]]

log = logging.getLogger(__name__)
_UNVERIFIED_INPUT_DELIVERY_DETAIL = (
    "Codex input delivery could not be verified; provider output was discarded."
)

_DYNAMIC_TOOL_DRAIN_TIMEOUT_S = 30.0


async def steer_restart_pending(
    thread_id: str,
    expected_turn_id: str,
    message: str,
) -> bool:
    """Steer one exact live Codex turn with restart control."""
    async with _LIVE.lock(thread_id):
        live = _LIVE.get(thread_id)
        if (
            live is None
            or live.corsia_turn is None
            or live.corsia_turn.turn_id != expected_turn_id
        ):
            return False
        steer = _async_method(live.session, "steer")
        if steer is None:
            return False
        return bool(await steer(message))


async def capture_retirement_owner(thread_id: str):
    """Freeze the exact reusable Codex owner under its transition lock."""
    lock = _LIVE.lock(thread_id)
    async with lock:
        owner = current_handle_fingerprint(thread_id)
        if owner is None:
            await _restore_thread_id(
                thread_id,
                None,
                validate_tool_surface=False,
            )
            owner = current_handle_fingerprint(thread_id)
        return owner


async def retire_if_current(
    thread_id: str,
    *,
    expected_owner,
    db=None,
    broadcast=None,
) -> bool:
    """Retire only the Codex owner captured before a context commit."""
    lock = _LIVE.lock(thread_id)
    async with lock:
        current_owner = current_handle_fingerprint(thread_id)
        if expected_owner is None:
            return current_owner is None
        if current_owner != expected_owner:
            return False
        return await _retire_locked(
            thread_id,
            db=db,
            broadcast=broadcast,
        )


async def retarget_if_current(
    thread_id: str,
    *,
    expected_owner,
    target: ExecutionTarget,
    runtime_context_epoch: int,
) -> bool:
    """Keep the captured Codex thread and select its next-turn model."""
    if target.harness != "codex":
        raise ValueError("Codex continuation cannot target another harness")
    lock = _LIVE.lock(thread_id)
    async with lock:
        current_owner = current_handle_fingerprint(thread_id)
        if expected_owner is None or current_owner != expected_owner:
            return False
        live = _LIVE.get(thread_id)
        session = live.session if live is not None else _APP_SESSIONS.get(thread_id)
        if session is not None:
            session.model = target.transport_model
            session.catalog_model_id = target.catalog_id
            session.expected_reported_model = target.expected_reported_model
            session.provider_reported_model = None
            session.runtime_context_epoch = runtime_context_epoch
        if live is not None:
            live.model = target.transport_model
            live.catalog_model_id = target.catalog_id
            live.harness = target.harness
            live.expected_reported_model = target.expected_reported_model
            live.provider_reported_model = None
            live.runtime_context_epoch = runtime_context_epoch
        return True


def _default_broadcast(thread_id: str, message: dict) -> None:
    from infra import state

    state.broadcast_to_thread(thread_id, message)


def _surface_exit_error(
    thread_id: str,
    code: int | None,
    stderr: str,
    bcast,
    db,
    turn_identity: TurnIdentity,
    *,
    terminal_applied: bool = False,
    emit_terminal: bool = True,
) -> str | None:
    return surface_exit_error(
        thread_id,
        code,
        stderr,
        bcast,
        db,
        turn_identity,
        terminal_applied=terminal_applied,
        emit_terminal=emit_terminal,
        insert_message_fn=insert_message,
        logger=log,
    )


def _require_owned_accept_db(db) -> None:
    if db is not None:
        raise RuntimeError("Codex async acceptance requires an owned DB connection")


async def _start_live_turn(
    thread_id: str,
    live: _LiveCodex,
    text: str,
    *,
    successor_turn_id: str,
    bcast,
    db=None,
    preadmitted_turn: TurnIdentity | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
) -> TurnIdentity:
    _require_owned_accept_db(db)

    def _bind_turn(identity: TurnIdentity) -> None:
        live.last_assistant_message_id = None
        live.thinking_buf = ""
        live.thinking_provenance = None
        live.last_text = ""
        live.turn_has_assistant_row = False
        live.dropped_line_this_turn = False
        live.corsia_turn = identity
        if live.session is not None:
            setattr(live.session, "corsia_turn_identity", identity)

    def _unbind_turn(identity: TurnIdentity) -> None:
        if live.corsia_turn == identity:
            live.corsia_turn = None
        if (
            live.session is not None
            and getattr(live.session, "corsia_turn_identity", None) == identity
        ):
            setattr(live.session, "corsia_turn_identity", None)

    if preadmitted_turn is not None:
        if preadmitted_turn.turn_id != successor_turn_id:
            raise ValueError("preadmitted Codex Turn does not match successor_turn_id")
        identity = preadmitted_turn
        await bind_preadmitted_turn(thread_id, identity, _bind_turn, _unbind_turn)
    else:
        identity = await persist_turn_start_bound(
            successor_turn_id, thread_id, live.model or "codex", text,
            _bind_turn, _unbind_turn,
        )
    if transfer_delivery_to_turn is not None:
        transfer_delivery_to_turn()
    bcast(thread_id, await turn_activity_frame(thread_id, identity))
    return identity


async def _claim_live_terminal(
    live: _LiveCodex,
    outcome: str,
    error: str | BaseException | None = None,
    db=None,
    *,
    identity: TurnIdentity | None = None,
) -> TurnTerminalResult:
    identity = identity or live.corsia_turn
    if identity is None:
        return TurnTerminalResult.CONFLICTING_TERMINAL
    if db is not None:
        return persist_turn_terminal(
            identity.turn_id,
            outcome,
            error,
            db=db,
        )
    return await settle_turn_terminal(identity.turn_id, outcome, error)


async def _end_owned_turn(
    thread_id: str,
    live: _LiveCodex,
    identity: TurnIdentity | None,
    outcome: str,
    *,
    bcast,
    source: str | None = None,
    error: str | BaseException | None = None,
    marker: Callable[[], Awaitable[str | None]] | None = None,
    marker_is_context_owner: bool = False,
    terminal_type: str = "turn_complete",
    db=None,
    defer_drive: bool = False,
) -> TurnTerminalResult | TerminalSettlement:
    """Settle tool results, commit this exact Turn, then publish its terminal."""
    if identity is None:
        if live.tool_step_ids:
            raise RuntimeError(
                f"Codex live {thread_id!r} owes tool results with no Turn to end"
            )
        return TurnTerminalResult.CONFLICTING_TERMINAL
    await _tool_terminal.settle_codex_results(thread_id, live, identity, error, bcast, db)
    await _settle_live_tool_debt(thread_id, live, bcast)
    if not defer_drive:
        terminal = await _claim_live_terminal(
            live, outcome, error, db, identity=identity,
        )
    else:
        assert db is None, "deferred Codex terminal requires its own transaction"
        terminal = await commit_turn_terminal(identity.turn_id, outcome, error)
    result = terminal.result if isinstance(terminal, TerminalSettlement) else terminal
    if result is not TurnTerminalResult.APPLIED:
        return terminal
    with preserve_committed_terminal(terminal):
        marker_id = await marker() if marker is not None else None
        if source is not None:
            await _publish_claimed_terminal(
                thread_id=thread_id, live=live, identity=identity, bcast=bcast,
                source=source, marker_id=marker_id,
                marker_is_context_owner=marker_is_context_owner,
                terminal_type=terminal_type, db=db, stop_error=error,
            )
    return terminal


async def _finish_rejected_input(
    thread_id: str,
    live: _LiveCodex,
    identity: TurnIdentity | None,
    error: str,
    bcast,
) -> TurnTerminalResult:
    """Terminalize and publish one definitively rejected provider input."""
    return await _end_owned_turn(
        thread_id,
        live,
        identity,
        "error",
        error=error,
        bcast=bcast,
        source="codex-app-server:input-rejected",
    )


async def _finish_unverified_input_delivery(
    thread_id: str,
    live: _LiveCodex,
    identity: TurnIdentity | None,
    bcast,
) -> TurnTerminalResult:
    """Close a turn whose provider write cannot be causally proved.

    The provider may still emit late events, but its triggering user row stays
    undelivered.  Those events cannot become CORSIA history; leaving the turn
    active while waiting for them would strand the thread if they never arrive.
    """
    if identity is None:
        return TurnTerminalResult.CONFLICTING_TERMINAL
    if live.tool_step_ids:
        session = live.session
        revoke = getattr(session, "revoke_dynamic_tool_admission", None)
        wait_idle = _async_method(session, "wait_dynamic_tool_requests_idle")
        if not callable(revoke) or wait_idle is None:
            raise RuntimeError(
                f"Codex unverified terminal for {thread_id} cannot fence tool debt"
            )
        revoke()
        await asyncio.wait_for(
            wait_idle(),
            timeout=_DYNAMIC_TOOL_DRAIN_TIMEOUT_S,
        )
        await asyncio.wait_for(
            live.tools_settled.wait(),
            timeout=_DYNAMIC_TOOL_DRAIN_TIMEOUT_S,
        )
        if _LIVE.get(thread_id) is not live or live.corsia_turn != identity:
            raise RuntimeError(
                f"Codex unverified terminal for {thread_id} lost exact ownership"
            )
        if live.tool_step_ids:
            raise RuntimeError(
                f"Codex unverified terminal for {thread_id} retained tool debt"
            )
    return await _end_owned_turn(
        thread_id,
        live,
        identity,
        "error",
        error=_UNVERIFIED_INPUT_DELIVERY_DETAIL,
        bcast=bcast,
        source="codex-app-server:input-delivery-unverified",
    )


async def _finish_process_exit(
    thread_id: str,
    live: _LiveCodex,
    code: int | None,
    stderr: str | BaseException,
    bcast,
    db=None,
    *,
    defer_drive: bool = False,
) -> TurnTerminalResult | TerminalSettlement:
    identity = live.corsia_turn
    detail = _tool_terminal.exit_detail(
        stderr, code, "Codex", live.tool_boundary_violation_recorded,
    )
    async def marker() -> str | None:
        """The exit's user-visible record, unless a boundary marker owns it."""
        if live.tool_boundary_marker_id is not None:
            return live.tool_boundary_marker_id
        return _surface_exit_error(
            thread_id,
            code,
            detail,
            bcast,
            db,
            identity,
            terminal_applied=True,
            emit_terminal=False,
        )

    return await _end_owned_turn(
        thread_id,
        live,
        identity,
        "error",
        error=_tool_terminal.retained_error(stderr, detail),
        bcast=bcast,
        source="codex-app-server:transport-exit",
        marker=marker,
        # cc_error rows are WS anchors, not eligible context owners.
        marker_is_context_owner=False,
        db=db,
        defer_drive=defer_drive,
    )


async def _settle_exited_generation(
    thread_id: str, live: _LiveCodex, session, code: int | None,
    stderr: str | BaseException, bcast,
) -> tuple[TerminalSettlement | None, BaseException | None]:
    try:
        terminal = await _finish_process_exit(
            thread_id, live, code, stderr, bcast, defer_drive=True,
        )
        publication_error = None
    except CommittedTerminalFailure as failure:
        terminal, publication_error = failure.settlement, failure.publication_error
    _retire_exited_generation(thread_id, live, session)
    return terminal if isinstance(terminal, TerminalSettlement) else None, publication_error


async def settle_dead_live_generation_if_observed(
    thread_id: str, *, retiring: _LiveCodex | None = None, error: str | BaseException = "",
) -> bool:
    """Terminalize one exact dead app-server owner outside its transition lock."""
    async with _LIVE.lock(thread_id):
        if (observed := dead_generation_ready_for_settlement(thread_id, retiring=retiring)) is None:
            return False
        live, session, returncode = observed
        settlement, publication_error = await _settle_exited_generation(
            thread_id, live, session, returncode, error, _default_broadcast)
    if settlement is not None: await turn_core.drive_turn_settlement(settlement)
    if publication_error is not None: raise publication_error
    return True


async def _finish_interrupted_turn(
    thread_id: str,
    live: _LiveCodex,
    cause: str,
    bcast, db=None,
    *,
    create_marker: bool = True,
    defer_drive: bool = False,
    stop_error: PlanLimitStop | None = None,
) -> TurnTerminalResult | TerminalSettlement:
    async def marker(identity: TurnIdentity | None = live.corsia_turn) -> str | None:
        assert identity is not None
        return await record_terminal_marker(
            thread_id,
            "",
            {
                "codex": True,
                "type": "result",
                "is_error": False,
                "stop_reason": "interrupted",
                "interrupted": True, "cause": cause, "turn_id": identity.turn_id,
            },
            bcast,
            db,
        )
    terminal_source = "codex-app-server:hard-interrupt" if create_marker else f"codex-app-server:lifecycle-{cause}"
    terminal_type = "turn_cancelled" if cause == "stop_requested" else "turn_complete"
    return await _end_owned_turn(
        thread_id,
        live,
        live.corsia_turn, "cancelled", error=stop_error,
        bcast=bcast,
        source=terminal_source,
        marker=marker if create_marker else None,
        marker_is_context_owner=True,
        terminal_type=terminal_type,
        db=db,
        defer_drive=defer_drive,
    )


async def _settle_and_release_delivery_event_barrier(
    thread_id: str,
    live: _LiveCodex,
    settle_delivery: Callable[[], Awaitable[None]] | None,
) -> None:
    """Settle input proof, then release any early app-server notifications."""
    try:
        if settle_delivery is not None:
            await _await_delivery_settlement(settle_delivery)
    except BaseException:
        _fail_delivery_event_barrier(thread_id, live)
        raise
    await _release_delivery_event_barrier(live)


async def _quarantine_provider_context(
    thread_id: str,
    live: _LiveCodex,
) -> bool:
    """Quarantine reuse handles only while this exact live still owns them."""
    if _LIVE.get(thread_id) is not live:
        return False
    live.provider_context_untrusted = True
    mark_provider_context_untrusted(thread_id)
    return True


async def _settle_live_tool_debt(
    thread_id: str,
    live: _LiveCodex,
    bcast,
) -> None:
    """Settle one live turn's exact durable tool debt before terminalization."""
    if not live.tool_step_ids:
        live.tools_settled.set()
        return
    identity = live.corsia_turn
    if identity is None:
        raise RuntimeError(
            f"Codex live {thread_id!r} has tools without a turn identity"
        )
    from subagents.codex_runtime_events import settle_missing_tool_results

    await settle_missing_tool_results(
        live=live,
        thread_id=thread_id,
        identity=identity,
        bcast=bcast,
    )
    if live.tool_step_ids or not live.tools_settled.is_set():
        raise RuntimeError(
            f"Codex live {thread_id!r} retained tool debt after settlement"
        )


async def _finish_transport_exit(
    thread_id: str,
    live: _LiveCodex,
    session,
    code: int | None,
    stderr: str | BaseException,
    bcast,
) -> None:
    if live.tool_step_ids and _LIVE.get(thread_id) is live:
        await _quarantine_provider_context(thread_id, live)
    settlement, publication_error = await _settle_exited_generation(
        thread_id, live, session, code, stderr, bcast,
    )
    if settlement is not None: await turn_core.drive_turn_settlement(settlement)
    if publication_error is not None: raise publication_error


async def _accept_app_server_input(
    thread_id: str,
    live: _LiveCodex,
    start_input: Callable[[Callable[[], None], Callable[[], None]], Awaitable[object]],
    *,
    mark_accepted: Callable[[], None] | None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    bcast,
) -> bool:
    """Accept one ``turn/start`` without erasing write-observed ambiguity."""
    ambiguous = False
    write_observed = asyncio.Event()
    _arm_delivery_event_barrier(live)
    def on_ambiguous(_drain_confirmed: bool) -> None:
        nonlocal ambiguous
        ambiguous = True
        live.provider_context_untrusted = True
        if _LIVE.get(thread_id) is not live:
            _PROVIDER_CONTEXT_UNTRUSTED.add(thread_id)
        if mark_ambiguous is not None:
            mark_ambiguous()
        _fail_delivery_event_barrier(thread_id, live)
    try:
        accepted = await _causal_app_server_request(
            start_input, on_ambiguous=on_ambiguous, on_write=write_observed.set,
        )
    except BaseException as exc:
        if not write_observed.is_set() and live.checkpoint_wait_scope.stop_requested:
            _discard_delivery_event_barrier(live)
            raise asyncio.CancelledError(
                "Codex Stop cancelled input before provider write"
            ) from exc
        if getattr(live.session, "_closing", False):
            _discard_delivery_event_barrier(live)
            raise
        if ambiguous:
            if settle_delivery is not None:
                await _await_delivery_settlement(settle_delivery)
            raise
        # A rejected request must not release a surprising pre-response event
        # from a provider that raced its own negative acknowledgement.
        _discard_delivery_event_barrier(live)
        raise
    if accepted is not True:
        if write_observed.is_set():
            on_ambiguous(False)
            if settle_delivery is not None:
                await _await_delivery_settlement(settle_delivery)
        _discard_delivery_event_barrier(live)
        if getattr(live.session, "_closing", False):
            raise RuntimeError("Codex app-server terminated during input acceptance")
        return False
    if mark_accepted is not None:
        mark_accepted()
    await _settle_and_release_delivery_event_barrier(thread_id, live, settle_delivery)
    return True


async def _spawn_turn(
    thread_id: str,
    text: str,
    *,
    successor_turn_id: str,
    current_input_message_id: str | None = None,
    codex_thread_id: str | None = None,
    binary: str | None = None,
    model: str | None = None,
    catalog_model_id: str | None = None,
    windows: WindowFacts = WindowFacts(None, None),
    expected_reported_model: str | None = None,
    harness: Harness = "codex",
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: _PreparedRefresh | None = None,
    working_dir: str | None = None,
    project_id: str | None = None,
    effort: str | None = None,
    broadcast=None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: _InputTerminalSettlement | None = None,
    continuing_wait_scope: object | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection=None,
    db=None,
    held_lock: asyncio.Lock,
) -> TurnIdentity | None:
    _require_owned_accept_db(db)
    binary = _spawn_binary(binary, db)
    payload = await run_in_db(_resolve_spawn_payload,
        thread_id=thread_id, text=text,
        current_input_message_id=current_input_message_id, codex_thread_id=codex_thread_id,
        catalog_model_id=catalog_model_id,
        context_window=windows.effective,
        prepared_context=prepared_context,
        working_dir=working_dir,
        project_id=project_id,
        db=None,
    )
    return await _spawn_app_server_turn(
        thread_id, payload.provider_text,
        current_user_text=text,
        successor_turn_id=successor_turn_id,
        codex_thread_id=codex_thread_id,
        binary=binary,
        working_dir=working_dir,
        project_id=project_id,
        effort=effort,
        broadcast=broadcast,
        context_snapshot=payload.reconstruction.snapshot.to_dict(),
        continuity_accounting=payload.continuity_accounting,
        history_items=payload.history_items,
        tool_registry=payload.tool_registry,
        dynamic_tools=payload.dynamic_tools,
        model=model,
        catalog_model_id=catalog_model_id,
        runtime_context_epoch=_spawn_runtime_epoch(prepared_context, thread_id, db),
        windows=windows,
        expected_reported_model=expected_reported_model,
        harness=harness,
        mark_accepted=mark_accepted, mark_ambiguous=mark_ambiguous,
        settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        continuing_wait_scope=continuing_wait_scope,
        preadmitted_turn=preadmitted_turn,
        media_projection=media_projection,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
        client_message_id=current_input_message_id, db=db, held_lock=held_lock,
    )


async def _finish_failed_app_server_spawn(
    thread_id: str,
    live: _LiveCodex,
    session,
    exc: BaseException,
    bcast,
    *,
    db,
    terminalize: bool,
    settle_delivery_terminal: _InputTerminalSettlement | None = None,
) -> None:
    """Settle admitted input and retire its exact runtime before successor drive."""
    from subagents.codex_app_server_errors import CodexAppServerError

    outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
    if settle_delivery_terminal is not None:
        await settle_delivery_terminal(outcome, turn_core.terminal_error_detail(exc) or None)
    if live.input_delivery_unproven:
        _capture_app_server_request(live, session)
        if _LIVE.get(thread_id) is live and session is not None:
            _APP_SESSIONS[thread_id] = session
    elif live.session is not None and not await _fence_exact_live(thread_id, live):
        raise RuntimeError("Codex failed admission could not fence its exact runtime") from exc
    _discard_delivery_event_barrier(live)
    terminal = None
    if terminalize:
        detail = _UNVERIFIED_INPUT_DELIVERY_DETAIL if live.input_delivery_unproven else str(exc)
        try:
            terminal = await _end_owned_turn(
                thread_id, live, live.corsia_turn, outcome,
                error=detail, bcast=bcast, source="codex-app-server:input-rejected",
                defer_drive=True,
            )
        except CommittedTerminalFailure:
            if not live.input_delivery_unproven:
                _release_exact_live(thread_id, live)
            raise
    with preserve_committed_terminal(terminal):
        if not live.input_delivery_unproven:
            _release_exact_live(thread_id, live)
        if isinstance(exc, CodexAppServerError) and exc.is_missing_thread_rollout:
            _CODEX_THREADS.pop(thread_id, None)
            _retire_registered_thread_artifacts(thread_id, db=db)
    if isinstance(terminal, TerminalSettlement):
        raise CommittedTerminalFailure(terminal, exc) from exc


async def _accept_spec_app_server_input(
    spec: _AppServerTurnSpec, live: _LiveCodex, start_input, bcast,
) -> bool:
    return await _accept_app_server_input(
        spec.thread_id, live, start_input,
        mark_accepted=spec.mark_accepted, mark_ambiguous=spec.mark_ambiguous,
        settle_delivery=spec.settle_delivery, bcast=bcast,
    )


async def _run_app_server_turn(spec: _AppServerTurnSpec) -> TurnIdentity | None:
    _require_owned_accept_db(spec.db)
    bcast = spec.broadcast or _default_broadcast
    session = _reusable_app_session(spec.thread_id)
    tool_registry, dynamic_tools = _resolve_app_server_tools(
        session, working_dir=spec.working_dir, tool_registry=spec.tool_registry,
        dynamic_tools=spec.dynamic_tools,
    )
    exact_model = _validate_codex_transport_model(spec.model)
    live = _new_app_server_live(
        session,
        codex_thread_id=spec.codex_thread_id,
        model=spec.model,
        catalog_model_id=spec.catalog_model_id,
        runtime_context_epoch=spec.runtime_context_epoch,
        windows=spec.windows,
        expected_reported_model=spec.expected_reported_model,
        context_snapshot=spec.context_snapshot,
        harness=spec.harness,
        continuity_accounting=spec.continuity_accounting,
        continuing_wait_scope=spec.continuing_wait_scope,
    )
    on_event = _make_on_event(
        live, spec.thread_id, bcast, _checkpoint_replay_id(spec.prepared_context))
    async def on_exit(code: int | None, stderr: str) -> None:
        await _finish_transport_exit(
            spec.thread_id, live, session, code, stderr, bcast,
        )
    _LIVE.publish(spec.thread_id, live, held=spec.held_lock)
    try:
        turn_id = await _start_live_turn(
            spec.thread_id, live, spec.text,
            successor_turn_id=spec.successor_turn_id,
            bcast=bcast,
            preadmitted_turn=spec.preadmitted_turn,
            transfer_delivery_to_turn=(
                spec.transfer_delivery_to_turn if spec.continuing_wait_scope is None else None
            ),
        )
        session, start_input = await _configure_spec_app_server_input(
            session, live, spec, exact_model=exact_model,
            tool_registry=tool_registry, dynamic_tools=dynamic_tools,
            on_event=on_event, on_exit=on_exit,
        )
        resumed = await _maintain_codex_initial_boundary(spec, live)
        _require_open_codex_initial_boundary(resumed, live)
        if resumed is not None:
            return resumed
        accepted = await _accept_spec_app_server_input(spec, live, start_input, bcast)
        if not accepted:
            raise RuntimeError("Codex did not confirm a provider turn")
        _stamp_app_server_context(session, spec)
        if _LIVE.get(spec.thread_id) is live:
            _APP_SESSIONS[spec.thread_id] = session
        _capture_app_server_request(live, session)
    except CommittedTerminalFailure:
        raise
    except BaseException as exc:
        if live.checkpoint_wait_scope.stop_requested:
            if spec.settle_delivery_terminal is not None:
                await spec.settle_delivery_terminal("cancelled", str(exc) or None)
            raise
        await _finish_failed_app_server_spawn(
            spec.thread_id,
            live,
            session,
            exc,
            bcast,
            db=spec.db,
            terminalize=live.corsia_turn is not None,
            settle_delivery_terminal=spec.settle_delivery_terminal,
        )
        raise
    _remember_spawned_app_server_turn(spec, live, session)
    return turn_id


async def _spawn_app_server_turn(
    thread_id: str,
    provider_text: str,
    *,
    current_user_text: str,
    successor_turn_id: str,
    codex_thread_id: str | None,
    binary: str,
    working_dir: str | None,
    broadcast,
    project_id: str | None = None,
    effort: str | None = None,
    context_snapshot: dict | None = None,
    continuity_accounting=None,
    history_items: tuple[dict[str, object], ...] = (),
    tool_registry=None,
    dynamic_tools: tuple[dict[str, object], ...] | None = None,
    model: str | None = None,
    catalog_model_id: str | None = None,
    runtime_context_epoch: int = 0,
    windows: WindowFacts = WindowFacts(None, None),
    expected_reported_model: str | None = None,
    harness: Harness = "codex",
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: _InputTerminalSettlement | None = None,
    continuing_wait_scope: object | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection=None,
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: _PreparedRefresh | None = None,
    client_message_id: str | None = None,
    db=None,
    held_lock: asyncio.Lock,
) -> TurnIdentity | None:
    spec = _AppServerTurnSpec(
        thread_id=thread_id, text=provider_text,
        current_user_text=current_user_text, successor_turn_id=successor_turn_id,
        codex_thread_id=codex_thread_id, binary=binary, working_dir=working_dir,
        broadcast=broadcast, project_id=project_id, effort=effort,
        context_snapshot=context_snapshot, continuity_accounting=continuity_accounting,
        history_items=history_items,
        tool_registry=tool_registry, dynamic_tools=dynamic_tools, model=model,
        catalog_model_id=catalog_model_id, runtime_context_epoch=runtime_context_epoch,
        windows=windows, expected_reported_model=expected_reported_model, harness=harness,
        mark_accepted=mark_accepted, mark_ambiguous=mark_ambiguous,
        settle_delivery=settle_delivery, client_message_id=client_message_id,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        continuing_wait_scope=continuing_wait_scope, preadmitted_turn=preadmitted_turn,
        media_projection=media_projection, prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context, db=db, held_lock=held_lock,
    )
    return await _run_app_server_turn(spec)


def is_live(thread_id: str) -> bool:
    return thread_id in _LIVE


def has_pending_or_live(thread_id: str) -> bool:
    """Return whether a Codex transition or callback-capable handle exists."""
    lock = _LIVE.existing_lock(thread_id)
    return (
        thread_id in _LIVE
        or thread_id in _APP_SESSIONS
        or (lock is not None and lock.locked())
    )


async def has_thread_id(thread_id: str, db=None) -> bool:
    """Return whether Codex has a live or persisted provider thread.

    The untrusted flag describes OUR view of the local process, not the
    existence of the provider's thread. Returning False for it reported "no
    handle" for a thread that was resumable, which is what forced a cold
    rebuild on every send after a provider compaction.
    """
    session = _APP_SESSIONS.get(thread_id)
    codex_thread_id = _CODEX_THREADS.get(thread_id) or getattr(session, "codex_thread_id", None)
    if codex_thread_id:
        _CODEX_THREADS[thread_id] = str(codex_thread_id)
        return True
    return bool(await _restore_thread_id(thread_id, db))


async def _await_dynamic_fence(
    thread_id: str,
    label: str,
    awaitable: Awaitable[object],
    *,
    deadline: float,
    restore: Callable[[], None],
) -> tuple[bool, object | None]:
    """Await one bounded fence stage and restore a surviving exact session."""
    timeout = max(0.0, deadline - asyncio.get_running_loop().time())
    try:
        return True, await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.CancelledError:
        restore()
        raise
    except Exception as exc:  # noqa: BLE001 — every failed stage retains owner
        log.error(
            "codex_runtime: %s %s fence failed: %s",
            thread_id,
            label,
            exc,
        )
        restore()
        return False, None


def _restore_dynamic_admission(
    thread_id: str,
    live: _LiveCodex,
    session,
    restore,
) -> None:
    owner = _LIVE.get(thread_id)
    if (
        _APP_SESSIONS.get(thread_id) is session
        and (owner is None or owner is live)
        and getattr(session, "returncode", None) is None
        and not live.provider_context_untrusted
        and thread_id not in _PROVIDER_CONTEXT_UNTRUSTED
    ):
        restore()


def _has_exact_live_owner(thread_id: str, live: _LiveCodex, stage: str) -> bool:
    owner = _LIVE.get(thread_id)
    if owner is None or owner is live:
        return True
    log.error(
        "codex_runtime: %s live owner changed at %s fence",
        thread_id,
        stage,
    )
    return False


async def _terminate_turn_foreground_process(
    thread_id: str,
    live: _LiveCodex,
    restore=None,
) -> bool:
    identity = live.corsia_turn
    if not isinstance(identity, TurnIdentity) or not _has_exact_live_owner(
        thread_id,
        live,
        "process cleanup",
    ):
        log.error(
            "codex_runtime: %s dynamic-tool live has no exact process identity",
            thread_id,
        )
        return False
    from host_access.processes import PROCESS_OWNER

    try:
        await PROCESS_OWNER.terminate_turn(
            thread_id,
            identity.turn_id,
            foreground_only=True,
            reason="codex_turn_interrupted",
        )
    except asyncio.CancelledError:
        if restore is not None:
            restore()
        raise
    except Exception as exc:
        log.error(
            "codex_runtime: %s foreground process cleanup failed: %s",
            thread_id,
            exc,
        )
        return False
    return _has_exact_live_owner(thread_id, live, "process cleanup")


async def _finish_dead_app_server_fence(
    thread_id: str,
    live: _LiveCodex,
    session,
    returncode: int,
    bcast,
    *,
    deadline: float,
    restore,
) -> bool:
    wait_reader_drained = _async_method(session, "wait_reader_drained")
    if wait_reader_drained is None:
        log.error(
            "codex_runtime: dead app-session has no reader-drained fence for %s",
            thread_id,
        )
        return False
    ok, _ = await _await_dynamic_fence(
        thread_id,
        "stdout reader",
        wait_reader_drained(),
        deadline=deadline,
        restore=restore,
    )
    if not ok or not _has_exact_live_owner(thread_id, live, "reader"):
        return False
    if _LIVE.get(thread_id) is None:
        return True
    if _APP_SESSIONS.get(thread_id) is not session:
        log.error(
            "codex_runtime: %s app-session owner changed at reader fence",
            thread_id,
        )
        return False
    try:
        await _finish_transport_exit(
            thread_id,
            live,
            session,
            returncode,
            "",
            bcast,
        )
        return True
    except Exception as exc:
        log.error(
            "codex_runtime: dead app-session recovery failed for %s: %s",
            thread_id,
            exc,
        )
        return False


async def _finish_live_app_server_fence(
    thread_id: str,
    live: _LiveCodex,
    interrupt,
    *,
    deadline: float,
    restore,
) -> bool:
    ok, _ = await _await_dynamic_fence(
        thread_id,
        "durable tool-result",
        live.tools_settled.wait(),
        deadline=deadline,
        restore=restore,
    )
    if not ok:
        return False
    if live.tool_step_ids:
        log.error("codex_runtime: %s durable tool rows remain", thread_id)
        restore()
        return False
    if _LIVE.get(thread_id) is None:
        return True
    if not _has_exact_live_owner(thread_id, live, "pre-terminal"):
        return False
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    ok, confirmed = await _await_dynamic_fence(
        thread_id,
        "provider terminal",
        interrupt(timeout=remaining),
        deadline=deadline,
        restore=restore,
    )
    if not ok or confirmed is not True:
        if ok:
            restore()
        return False
    if not _has_exact_live_owner(thread_id, live, "terminal"):
        return False
    if live.tool_step_ids or not live.tools_settled.is_set():
        log.error(
            "codex_runtime: %s tool ownership reappeared at terminal fence",
            thread_id,
        )
        restore()
        return False
    return True


async def _quiesce_app_server_tool_turn(
    thread_id: str,
    live: "_LiveCodex",
    bcast,
) -> bool | None:
    """Fence admitted request, durable result, then provider terminal."""
    from functools import partial

    session = live.session
    revoke = getattr(session, "revoke_dynamic_tool_admission", None)
    restore_admission = getattr(session, "restore_dynamic_tool_admission", None)
    wait_idle = _async_method(session, "wait_dynamic_tool_requests_idle")
    interrupt = _async_method(session, "interrupt")
    if not callable(revoke) or not callable(restore_admission) or wait_idle is None:
        return None
    restore = partial(
        _restore_dynamic_admission,
        thread_id,
        live,
        session,
        restore_admission,
    )
    revoke()
    if interrupt is None:
        log.error(
            "codex_runtime: %s dynamic-tool live has no interrupt fence",
            thread_id,
        )
        restore()
        return False
    if not await _terminate_turn_foreground_process(thread_id, live, restore):
        return False

    deadline = asyncio.get_running_loop().time() + _DYNAMIC_TOOL_DRAIN_TIMEOUT_S
    ok, _ = await _await_dynamic_fence(
        thread_id,
        "dynamic request",
        wait_idle(),
        deadline=deadline,
        restore=restore,
    )
    if not ok or not _has_exact_live_owner(thread_id, live, "request"):
        return False

    returncode = getattr(session, "returncode", None)
    if returncode is not None:
        return await _finish_dead_app_server_fence(
            thread_id,
            live,
            session,
            returncode,
            bcast,
            deadline=deadline,
            restore=restore,
        )
    return await _finish_live_app_server_fence(
        thread_id,
        live,
        interrupt,
        deadline=deadline,
        restore=restore,
    )


async def _quiesce_tool_turn(
    thread_id: str,
    live: "_LiveCodex",
    bcast,
    *,
    db=None,
) -> bool | None:
    """Quiesce one managed app-server turn at its dynamic-tool boundary."""
    return await _quiesce_app_server_tool_turn(thread_id, live, bcast)


async def _fence_exact_live(thread_id: str, live: "_LiveCodex") -> bool:
    """Stop the exact provider process while its handle owns the Turn."""
    live.provider_context_untrusted = True
    terminate = _async_method(live.session, "terminate")
    if terminate is None:
        log.error(
            "codex_runtime: drained live %s has no terminate method",
            thread_id,
        )
        return False
    try:
        await terminate()
    except Exception as exc:  # noqa: BLE001 — unproven death retains evidence
        log.error(
            "codex_runtime: terminate failed for drained live %s: %s",
            thread_id,
            exc,
        )
        return False
    current = _LIVE.get(thread_id)
    if current is not None and current is not live:
        log.error(
            "codex_runtime: %s live owner changed during exact termination",
            thread_id,
        )
        return False
    return True


def _release_exact_live(thread_id: str, live: "_LiveCodex") -> None:
    """Release only the exact local handle after its Turn is terminal."""
    current = _LIVE.get(thread_id)
    finalize_live(thread_id, live)
    if current is not None and current is not live:
        return
    if _APP_SESSIONS.get(thread_id) is live.session:
        _APP_SESSIONS.pop(thread_id, None)


async def _terminate_exact_live(thread_id: str, live: "_LiveCodex") -> bool:
    """Terminate and remove only the exact live proven terminal and drained."""
    if not await _fence_exact_live(thread_id, live):
        return False
    _release_exact_live(thread_id, live)
    return True


async def _finish_fenced_live(
    thread_id: str,
    live: _LiveCodex,
    cause: str,
    bcast,
    *,
    db=None,
) -> bool:
    if not await _finish_interrupted_if_still_owned(
        thread_id,
        live,
        cause,
        bcast,
        db=db,
    ):
        return False
    return await _terminate_exact_live(thread_id, live)


async def _fence_hard_preempted_live(
    thread_id: str,
    live: _LiveCodex,
    bcast,
    *,
    db,
) -> bool:
    drain_result = await _quiesce_tool_turn(thread_id, live, bcast, db=db)
    if drain_result is False:
        return False
    if await _finish_fenced_live(
        thread_id,
        live,
        "interrupt_unconfirmed",
        bcast,
        db=db,
    ):
        return True
    log.error(
        "codex_runtime: %s old runtime not proven dead; refusing to start a "
        "parallel turn",
        thread_id,
    )
    return False


async def _hard_preempt_live_turn(
    thread_id: str,
    live: "_LiveCodex",
    *,
    successor_turn_id: str,
    current_input_message_id: str | None = None,
    text: str,
    project_id: str | None,
    model: str | None,
    catalog_model_id: str | None = None,
    windows: WindowFacts = WindowFacts(None, None),
    expected_reported_model: str | None = None,
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: _PreparedRefresh | None = None,
    binary: str | None = None,
    effort: str | None = None,
    broadcast=None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: _InputTerminalSettlement | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    db=None,
    held_lock: asyncio.Lock,
) -> TurnIdentity | None:
    """Terminalize, quarantine, and replace an uninterruptible turn."""
    _require_owned_accept_db(db)
    bcast = broadcast or _default_broadcast
    if not await _fence_hard_preempted_live(thread_id, live, bcast, db=db):
        return None
    _CODEX_THREADS.pop(thread_id, None)
    _retire_registered_thread_artifacts(thread_id, db=db)
    resolved_project_id = (
        project_id if project_id is not None else get_thread_project_id(thread_id, db=db)
    )
    working_dir = _working_dir_for_reconstruction(resolved_project_id, db=db)
    prepared_context = await _refresh_prepared_codex_context_after_boundary(
        thread_id,
        target_catalog_id=catalog_model_id or "",
        current_user_text=text,
        previous_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
    )
    replacement = await _spawn_turn(
        thread_id,
        text,
        successor_turn_id=successor_turn_id,
        current_input_message_id=current_input_message_id,
        codex_thread_id=None,
        binary=binary,
        model=model,
        catalog_model_id=catalog_model_id,
        windows=windows,
        expected_reported_model=expected_reported_model,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
        effort=effort,
        broadcast=broadcast,
        working_dir=working_dir,
        project_id=resolved_project_id,
        mark_accepted=mark_accepted,
        mark_ambiguous=mark_ambiguous,
        settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        preadmitted_turn=preadmitted_turn,
        db=db, held_lock=held_lock,
    )
    if replacement is not None:
        current = _LIVE.get(thread_id)
        if current is not None and not current.provider_context_untrusted:
            _PROVIDER_CONTEXT_UNTRUSTED.discard(thread_id)
    return replacement


async def _live_provider_text(
    thread_id: str,
    text: str,
    live: _LiveCodex,
    *,
    catalog_model_id: str | None,
    prepared_context: PreparedContextPayload | None,
    refresh_prepared_context: _PreparedRefresh | None,
) -> str:
    prepared_context, destination = await _resolve_prepared_codex_context(
        thread_id,
        target_catalog_id=catalog_model_id or "",
        current_user_text=text,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
    )
    if prepared_context is not None:
        live.runtime_context_epoch = prepared_context.runtime_context_epoch
        if live.session is not None:
            live.session.runtime_context_epoch = live.runtime_context_epoch
    return destination.provider_text if destination is not None else text


def _live_model_switch_requested(
    live: _LiveCodex,
    *,
    model: str | None,
    expected_reported_model: str | None,
    preadmitted_turn: TurnIdentity | None,
) -> bool:
    return bool(
        preadmitted_turn is not None
        or (model and live.model != model)
        or (
            expected_reported_model is not None
            and live.expected_reported_model not in (None, expected_reported_model)
        )
        or (
            expected_reported_model is not None
            and live.provider_reported_model not in (None, expected_reported_model)
        )
    )


def _accept_ambiguous_live_steer(
    thread_id: str,
    live: _LiveCodex,
    session,
    state: dict[str, bool],
    mark_ambiguous: Callable[[], None] | None,
    _drain_confirmed: bool,
) -> None:
    state["ambiguous"] = True
    live.provider_context_untrusted = True
    if _LIVE.get(thread_id) is not live:
        _PROVIDER_CONTEXT_UNTRUSTED.add(thread_id)
    _capture_app_server_request(live, session)
    if mark_ambiguous is not None:
        mark_ambiguous()
    _fail_delivery_event_barrier(thread_id, live)


async def _steer_live_input(
    thread_id: str,
    live: _LiveCodex,
    provider_text: str,
    *,
    media_projection,
    bind_input_turn: Callable[[str, str | None], Awaitable[None]],
    mark_accepted: Callable[[], None] | None,
    mark_ambiguous: Callable[[], None] | None,
    settle_delivery: Callable[[], Awaitable[None]] | None,
    bcast,
    client_message_id: str | None = None,
) -> bool:
    from functools import partial
    from subagents.codex_app_server import CodexAppServerError

    session = live.session
    steer = _async_method(session, "steer")
    if steer is None:
        log.warning(
            "codex_runtime: session %s has a live turn without steer support", thread_id,
        )
        return False
    await bind_input_turn(live.corsia_turn.turn_id, None)
    state = {"ambiguous": False}
    _arm_delivery_event_barrier(live)

    async def steer_input(on_write, on_drain):
        kwargs = {"on_write_observed": on_write, "on_drain_confirmed": on_drain}
        optional = {"media_projection": media_projection, "client_message_id": client_message_id}
        kwargs.update({key: value for key, value in optional.items() if value is not None})
        return await steer(provider_text, **kwargs)

    try:
        steered = await _causal_app_server_request(
            steer_input,
            on_ambiguous=partial(
                _accept_ambiguous_live_steer,
                thread_id,
                live,
                session,
                state,
                mark_ambiguous,
            ),
        )
        if steered is True:
            if mark_accepted is not None:
                mark_accepted()
            await _settle_and_release_delivery_event_barrier(
                thread_id, live, settle_delivery,
            )
            return True
        _discard_delivery_event_barrier(live)
    except BaseException as exc:
        if state["ambiguous"] or live.input_delivery_unproven:
            if state["ambiguous"] and settle_delivery is not None:
                await _await_delivery_settlement(settle_delivery)
            await _finish_unverified_input_delivery(
                thread_id,
                live,
                live.corsia_turn,
                bcast,
            )
            raise
        _discard_delivery_event_barrier(live)
        if isinstance(exc, asyncio.CancelledError):
            raise
        if not isinstance(exc, CodexAppServerError) or not exc.is_not_steerable:
            raise
        log.warning(
            "codex_runtime: live turn for %s rejected steer as stale/not steerable: %s",
            thread_id, exc,
        )
    return False


# A declined steer preserves the live turn; terminal promotion owns its successor.
# Explicit Stop is destructive, so surface busy instead of a misleading ``None``.
async def _feed_live(
    thread_id: str,
    text: str,
    *,
    successor_turn_id: str,
    media_projection=None,
    current_input_message_id: str | None = None,
    bind_input_turn: Callable[[str, str | None], Awaitable[None]],
    project_id: str | None = None,
    binary: str | None = None,
    model: str | None = None,
    catalog_model_id: str | None = None,
    expected_reported_model: str | None = None,
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: _PreparedRefresh | None = None,
    effort: str | None = None,
    broadcast=None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    db=None,
) -> TurnIdentity | None:
    _require_owned_accept_db(db)
    bcast = broadcast or _default_broadcast
    live = _LIVE.get(thread_id)
    if live is not None and live.provider_context_untrusted:
        log.warning(
            "codex_runtime: refusing input for quarantined runtime %s",
            thread_id,
        )
        return None
    if live is None or live.corsia_turn is None:
        log.error(
            "codex_runtime: refusing live input without a bound CORSIA Turn %s",
            thread_id,
        )
        return None
    provider_text = await _live_provider_text(
        thread_id,
        text,
        live,
        catalog_model_id=catalog_model_id,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
    )
    if _async_method(live.session, "steer") is None:
        log.warning("codex_runtime: live session %s has no steer support", thread_id)
        return None
    switching_model = _live_model_switch_requested(
        live,
        model=model,
        expected_reported_model=expected_reported_model,
        preadmitted_turn=preadmitted_turn,
    )
    if not switching_model and await _steer_live_input(
        thread_id,
        live,
        provider_text,
        media_projection=media_projection,
        bind_input_turn=bind_input_turn,
        mark_accepted=mark_accepted,
        mark_ambiguous=mark_ambiguous,
        settle_delivery=settle_delivery,
        bcast=bcast,
        client_message_id=current_input_message_id,
    ):
        return live.corsia_turn
    if switching_model:
        log.info(
            "codex_runtime: deferring live model switch %s -> %s until the "
            "current turn relinquishes ownership",
            live.model,
            model,
        )
    raise CodexLiveTurnBusy(
        "Codex still owns the previous Turn; the input remains in the message FIFO"
    )


async def _retire_compacted_provider_context(
    thread_id: str,
    lock: asyncio.Lock,
    *,
    db,
    broadcast,
) -> None:
    if thread_id not in _PROVIDER_CONTEXT_UNTRUSTED:
        return
    async with lock:
        if thread_id not in _PROVIDER_CONTEXT_UNTRUSTED:
            return
        log.warning(
            "codex_runtime: %s provider-local context compacted; retiring "
            "the local process and resuming its provider thread",
            thread_id,
        )
        await _retire_locked(
            thread_id,
            db=db,
            broadcast=broadcast,
            keep_resume_handle=True,
        )
        _PROVIDER_CONTEXT_UNTRUSTED.discard(thread_id)


async def _feed_existing_codex(
    thread_id: str,
    text: str,
    *,
    target: ExecutionTarget,
    successor_turn_id: str,
    media_projection,
    current_input_message_id: str | None,
    bind_input_turn,
    project_id: str | None,
    binary: str | None,
    effort: str | None,
    prepared_context: PreparedContextPayload | None,
    refresh_prepared_context,
    broadcast,
    mark_accepted,
    mark_ambiguous,
    settle_delivery,
    preadmitted_turn,
    db,
) -> TurnIdentity | None:
    if effort:
        log.info(
            "codex_runtime: effort=%r ignored for live session %s; applies on "
            "next respawn",
            effort,
            thread_id,
        )
    return await _feed_live(
        thread_id,
        text,
        successor_turn_id=successor_turn_id,
        media_projection=media_projection,
        current_input_message_id=current_input_message_id,
        bind_input_turn=bind_input_turn,
        project_id=project_id,
        binary=binary,
        model=target.transport_model,
        catalog_model_id=target.catalog_id,
        expected_reported_model=target.expected_reported_model,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
        effort=effort,
        broadcast=broadcast,
        db=db,
        mark_accepted=mark_accepted,
        mark_ambiguous=mark_ambiguous,
        settle_delivery=settle_delivery,
        preadmitted_turn=preadmitted_turn,
    )


async def _spawn_missing_codex(
    thread_id: str,
    text: str,
    *,
    allow_fresh: bool,
    target: ExecutionTarget,
    successor_turn_id: str,
    current_input_message_id: str | None,
    bind_input_turn,
    project_id: str | None,
    binary: str | None,
    effort: str | None,
    prepared_context: PreparedContextPayload | None,
    refresh_prepared_context,
    broadcast,
    mark_accepted,
    mark_ambiguous,
    settle_delivery,
    transfer_delivery_to_turn,
    settle_delivery_terminal,
    preadmitted_turn,
    media_projection,
    db,
    held_lock: asyncio.Lock,
) -> TurnIdentity | None:
    codex_thread_id = _CODEX_THREADS.get(thread_id) or await _restore_thread_id(
        thread_id,
        db,
    )
    if not codex_thread_id and not allow_fresh:
        log.warning(
            "codex_runtime: no thread_id for session %s and a fresh start is "
            "not allowed",
            thread_id,
        )
        return None
    working_dir = _working_dir_for_reconstruction(project_id, db=db)
    await bind_input_turn(successor_turn_id, None)
    return await _spawn_turn(
        thread_id, text, successor_turn_id=successor_turn_id,
        current_input_message_id=current_input_message_id,
        codex_thread_id=codex_thread_id, binary=binary, model=target.transport_model,
        catalog_model_id=target.catalog_id, harness=target.harness, windows=target.windows,
        effort=effort, prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context, broadcast=broadcast,
        working_dir=working_dir, expected_reported_model=target.expected_reported_model,
        project_id=project_id, mark_accepted=mark_accepted,
        mark_ambiguous=mark_ambiguous, settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        preadmitted_turn=preadmitted_turn, media_projection=media_projection,
        db=db, held_lock=held_lock,
    )


async def _prepare_dispatch_context(
    thread_id: str,
    text: str,
    *,
    catalog_model_id: str,
    prepared_context: PreparedContextPayload | None,
    refresh_prepared_context,
    broadcast,
    db,
) -> tuple[PreparedContextPayload | None, bool]:
    """Return the prepared payload and its canonical cold-start decision."""
    prepared_context, destination = await _resolve_prepared_codex_context(
        thread_id,
        target_catalog_id=catalog_model_id,
        current_user_text=text,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
        allow_cold_with_handle=True,
        refresh_admission_boundary=True,
    )
    cold_start = (
        destination is not None and destination.delivery_mode != "provider_resume"
    )
    if not cold_start:
        return prepared_context, False
    if thread_id in _LIVE:
        raise CodexLiveTurnBusy(
            "Codex still owns the previous Turn; the input remains in the message FIFO"
        )
    has_resume_handle = thread_id in _APP_SESSIONS or thread_id in _CODEX_THREADS
    if has_resume_handle and not await _retire_locked(
        thread_id, db=db, broadcast=broadcast,
    ):
        raise CodexPreparedContextStale(
            "Codex could not retire the prior handle for its admitted cold payload"
        )
    return prepared_context, True


async def _dispatch_prepared_codex(
    thread_id: str,
    text: str,
    *,
    allow_fresh: bool,
    target: ExecutionTarget,
    successor_turn_id: str,
    media_projection,
    current_input_message_id: str | None,
    bind_input_turn,
    project_id: str | None,
    binary: str | None,
    effort: str | None,
    prepared_context: PreparedContextPayload | None,
    refresh_prepared_context,
    broadcast,
    mark_accepted,
    mark_ambiguous,
    settle_delivery,
    transfer_delivery_to_turn,
    settle_delivery_terminal,
    preadmitted_turn,
    db,
    held_lock: asyncio.Lock,
) -> TurnIdentity | None:
    if thread_id in _LIVE:
        return await _feed_existing_codex(
            thread_id,
            text,
            target=target,
            successor_turn_id=successor_turn_id,
            media_projection=media_projection,
            current_input_message_id=current_input_message_id,
            bind_input_turn=bind_input_turn,
            project_id=project_id,
            binary=binary,
            effort=effort,
            prepared_context=prepared_context,
            refresh_prepared_context=refresh_prepared_context,
            broadcast=broadcast,
            mark_accepted=mark_accepted,
            mark_ambiguous=mark_ambiguous,
            settle_delivery=settle_delivery,
            preadmitted_turn=preadmitted_turn,
            db=db,
        )
    return await _spawn_missing_codex(
        thread_id, text, allow_fresh=allow_fresh, target=target,
        successor_turn_id=successor_turn_id, media_projection=media_projection,
        current_input_message_id=current_input_message_id,
        bind_input_turn=bind_input_turn, project_id=project_id, binary=binary,
        effort=effort, prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context, broadcast=broadcast,
        mark_accepted=mark_accepted, mark_ambiguous=mark_ambiguous,
        settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        preadmitted_turn=preadmitted_turn, db=db, held_lock=held_lock,
    )


async def ensure_live_then_feed(
    thread_id: str,
    text: str,
    *,
    allow_fresh: bool,
    target: ExecutionTarget,
    media_projection=None,
    successor_turn_id: str,
    current_input_message_id: str | None = None,
    bind_input_turn: Callable[[str, str | None], Awaitable[None]],
    project_id: str | None = None,
    binary: str | None = None,
    effort: str | None = None,
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: _PreparedRefresh | None = None,
    broadcast=None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: _InputTerminalSettlement | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    db=None,
) -> TurnIdentity | None:
    """Steer, resume, or start one Codex turn under its transition lock."""
    _require_owned_accept_db(db)
    if target.harness != "codex":
        raise ValueError("Codex runtime received a non-Codex execution target")
    lock = _LIVE.lock(thread_id)
    await _retire_compacted_provider_context(
        thread_id,
        lock,
        db=db,
        broadcast=broadcast,
    )
    try:
        async with lock:
            if thread_id in _DEAD:
                log.warning("codex_runtime: session %s is dead/archived", thread_id)
                return None
            prepared_context, admitted_cold_start = await _prepare_dispatch_context(
                thread_id, text, catalog_model_id=target.catalog_id,
                prepared_context=prepared_context,
                refresh_prepared_context=refresh_prepared_context,
                broadcast=broadcast, db=db,
            )
            return await _dispatch_prepared_codex(
                thread_id, text, allow_fresh=allow_fresh or admitted_cold_start,
                target=target, successor_turn_id=successor_turn_id,
                current_input_message_id=current_input_message_id,
                bind_input_turn=bind_input_turn, project_id=project_id,
                binary=binary, effort=effort, prepared_context=prepared_context,
                refresh_prepared_context=refresh_prepared_context,
                broadcast=broadcast, mark_accepted=mark_accepted,
                mark_ambiguous=mark_ambiguous, settle_delivery=settle_delivery,
                transfer_delivery_to_turn=transfer_delivery_to_turn,
                settle_delivery_terminal=settle_delivery_terminal,
                preadmitted_turn=preadmitted_turn, media_projection=media_projection,
                db=db, held_lock=lock,
            )
    except CommittedTerminalFailure as failure:
        if failure.settlement is not None:
            await turn_core.drive_turn_settlement(failure.settlement)
        raise failure.publication_error


async def feed(
    thread_id: str,
    text: str,
    *,
    project_id: str | None = None,
    effort: str | None = None,
    target: ExecutionTarget,
    successor_turn_id: str,
    current_input_message_id: str | None = None,
    bind_input_turn: Callable[[str, str | None], Awaitable[None]],
    broadcast=None,
    db=None,
    agent_meta: dict | None = None,
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: _PreparedRefresh | None = None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: _InputTerminalSettlement | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection=None,
) -> TurnIdentity | None:
    """Continue an existing Codex thread (resume only; no fresh start)."""
    return await ensure_live_then_feed(
        thread_id,
        text,
        allow_fresh=False,
        project_id=project_id,
        effort=effort,
        successor_turn_id=successor_turn_id,
        current_input_message_id=current_input_message_id,
        bind_input_turn=bind_input_turn,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
        broadcast=broadcast,
        db=db,
        target=target,
        mark_accepted=mark_accepted,
        mark_ambiguous=mark_ambiguous,
        settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        preadmitted_turn=preadmitted_turn,
        media_projection=media_projection,
    )


async def feed_main(
    thread_id: str,
    text: str,
    *,
    target: ExecutionTarget,
    successor_turn_id: str,
    current_input_message_id: str | None = None,
    bind_input_turn: Callable[[str, str | None], Awaitable[None]],
    project_id: str | None = None,
    binary: str | None = None,
    effort: str | None = None,
    broadcast=None,
    db=None,
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: _PreparedRefresh | None = None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: _InputTerminalSettlement | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection=None,
) -> TurnIdentity | None:
    """Start or resume the Codex thread for a main / fresh turn."""
    return await ensure_live_then_feed(
        thread_id,
        text,
        allow_fresh=True,
        project_id=project_id,
        successor_turn_id=successor_turn_id,
        current_input_message_id=current_input_message_id,
        bind_input_turn=bind_input_turn,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
        target=target,
        binary=binary,
        effort=effort,
        broadcast=broadcast,
        db=db,
        mark_accepted=mark_accepted,
        mark_ambiguous=mark_ambiguous,
        settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        preadmitted_turn=preadmitted_turn,
        media_projection=media_projection,
    )


def _retire_registered_thread_artifacts(
    thread_id: str, db=None, *, keep_thread_id: str | None = None,
) -> bool:
    from registry import models

    with write_transaction(db, scope="retire_codex_thread_artifacts") as _db:
        rows = _db.execute(
            """SELECT i.id,i.external_id
               FROM artifact_identities i
               JOIN artifact_registrations r ON r.artifact_id=i.id
               WHERE r.parent_thread_id=? AND i.integration='codex'
                 AND i.type='thread' AND i.state='active'
                 AND r.relationship_state IN ('owner-pinned', 'owner-active')
                 AND (? IS NULL OR i.external_id<>?)""",
            (thread_id, keep_thread_id, keep_thread_id),
        ).fetchall()
        for row in rows:
            models.retire(row["id"], db=_db)
        return bool(rows)


async def _retire_locked(
    thread_id: str,
    *,
    db=None,
    broadcast=None,
    keep_resume_handle: bool = False,
) -> bool:
    """Retire local state, optionally preserving the durable provider thread."""
    changed = False
    live = _LIVE.get(thread_id)
    bcast = broadcast or _default_broadcast
    drain_result = (
        await _quiesce_tool_turn(thread_id, live, bcast, db=db)
        if live is not None
        else None
    )
    if drain_result is False:
        return False
    if live is not None and drain_result is True:
        if not await _finish_fenced_live(
            thread_id,
            live,
            "retire_requested",
            bcast,
            db=db,
        ):
            return False
    if live is not None and drain_result is None:
        identity = live.corsia_turn
        if identity is not None:
            await _finish_interrupted_turn(
                thread_id,
                live,
                "retire_requested",
                bcast,
                db,
                create_marker=False,
            )
        live.provider_context_untrusted = True
    live_session = getattr(live, "session", None) if live is not None else None
    app_session = _APP_SESSIONS.get(thread_id)
    if app_session is not None:
        changed = True
        if app_session is not live_session:
            if not await _terminate_app_session(thread_id):
                return False
    if live is not None:
        changed = True
        if drain_result is not True:
            from subagents.lifecycle import terminate_and_pop

            await terminate_and_pop(
                thread_id,
                live_registry=_LIVE,
                extra_pops=[_CODEX_THREADS, _APP_SESSIONS],
            )
    if not keep_resume_handle:
        if _CODEX_THREADS.pop(thread_id, None) is not None:
            changed = True
        changed = await run_in_db_unless_owned(db, _retire_registered_thread_artifacts, thread_id, db=db) or changed
    return changed


async def retire(thread_id: str, *, db=None, broadcast=None) -> bool:
    """Clear Codex continuation state without permanently marking it dead.

    Used when CORSIA's source-of-truth context changes underneath a session:
    model switch, role/scope assignment change, or message history truncation.
    A later send may start a reconstructed Codex provider thread from CORSIA
    DB truth, but it must not reuse the stale app-server session or captured
    thread id.
    """
    lock = _LIVE.lock(thread_id)
    async with lock:
        return await _retire_locked(
            thread_id,
            db=db,
            broadcast=broadcast,
        )


async def _mark_dead_locked(thread_id: str, *, broadcast=None) -> None:
    """Permanently retire one Codex continuation under its transition lock."""
    live = _LIVE.get(thread_id)
    bcast = broadcast or _default_broadcast
    drain_result = (
        await _quiesce_tool_turn(thread_id, live, bcast) if live is not None else None
    )
    if drain_result is False:
        return
    if live is not None and drain_result is True:
        if not await _finish_fenced_live(
            thread_id,
            live,
            "mark_dead_requested",
            bcast,
        ):
            return
        _DEAD.add(thread_id)
        _CODEX_THREADS.pop(thread_id, None)
        await run_in_db(_retire_registered_thread_artifacts, thread_id)
        return

    _DEAD.add(thread_id)
    if live is not None:
        identity = live.corsia_turn
        if identity is not None:
            await _finish_interrupted_turn(
                thread_id,
                live,
                "mark_dead_requested",
                bcast,
                create_marker=False,
            )
        live.provider_context_untrusted = True
    if not await _terminate_app_session(thread_id):
        return
    from subagents.lifecycle import terminate_and_pop

    await terminate_and_pop(
        thread_id,
        live_registry=_LIVE,
        dead_set=_DEAD,
        extra_pops=[_CODEX_THREADS, _APP_SESSIONS],
    )
    await run_in_db(_retire_registered_thread_artifacts, thread_id)


async def mark_dead(thread_id: str, *, broadcast=None) -> None:
    lock = _LIVE.lock(thread_id)
    async with lock:
        await _mark_dead_locked(thread_id, broadcast=broadcast)


async def _force_shutdown_unsettled_app_server(
    thread_id: str,
    live: _LiveCodex,
    bcast,
) -> None:
    """Kill one fenced app-server reader, then settle its durable tool debt."""
    session = live.session
    current = _LIVE.get(thread_id)
    if current is not None and current is not live:
        raise RuntimeError(
            f"Codex live owner changed before forced shutdown: {thread_id}"
        )
    if _APP_SESSIONS.get(thread_id) is not session:
        raise RuntimeError(
            f"Codex app-server owner changed before forced shutdown: {thread_id}"
        )
    revoke = getattr(session, "revoke_dynamic_tool_admission", None)
    wait_requests_idle = _async_method(
        session,
        "wait_dynamic_tool_requests_idle",
    )
    terminate = _async_method(session, "terminate")
    if not callable(revoke) or wait_requests_idle is None or terminate is None:
        raise RuntimeError(
            f"Codex app-server cannot force-stop exact reader: {thread_id}"
        )
    revoke()
    live.provider_context_untrusted = True
    await terminate()
    await asyncio.wait_for(
        wait_requests_idle(),
        timeout=_DYNAMIC_TOOL_DRAIN_TIMEOUT_S,
    )
    current = _LIVE.get(thread_id)
    if current is not None and current is not live:
        raise RuntimeError(
            f"Codex live owner changed during forced shutdown: {thread_id}"
        )
    code = getattr(session, "returncode", None)
    await _finish_transport_exit(
        thread_id,
        live,
        session,
        code,
        "Codex app-server was force-terminated during backend shutdown after "
        "its dynamic tool did not settle.",
        bcast,
    )


def _record_backend_shutdown_marker(thread_id: str) -> None:
    try:
        insert_message(
            thread_id,
            "system",
            "Terminated by backend shutdown.",
            {
                "codex": True,
                "type": "cc_error",
                "event_type": "harness.error",
                "harness": "codex",
                "cause": "backend_shutdown",
                "shutdown": True,
            },
            msg_id=str(uuid.uuid4()),
        )
    except Exception as exc:
        log.warning(
            "codex_runtime: shutdown marker failed for %s: %s",
            thread_id,
            exc,
        )


async def _shutdown_live_owner(
    thread_id: str,
    live: _LiveCodex,
    bcast,
) -> None:
    from subagents.lifecycle import terminate_and_pop

    drain_result = await _quiesce_tool_turn(thread_id, live, bcast)
    if _LIVE.get(thread_id) is not live:
        return
    if drain_result is False:
        if _APP_SESSIONS.get(thread_id) is not live.session:
            raise RuntimeError(f"Codex app-server tool settlement failed: {thread_id}")
        await _force_shutdown_unsettled_app_server(thread_id, live, bcast)
        return
    terminal = await _finish_interrupted_turn(
        thread_id,
        live,
        "backend_shutdown",
        bcast,
        create_marker=False,
    )
    if _LIVE.get(thread_id) is not live:
        return
    if drain_result is True:
        cleaned = await _terminate_exact_live(thread_id, live)
    else:
        cleaned = await terminate_and_pop(
            thread_id,
            live_registry=_LIVE,
            extra_pops=[_APP_SESSIONS, _CODEX_THREADS],
        )
    if not cleaned:
        raise RuntimeError(f"Codex exact live survived shutdown: {thread_id}")
    if terminal is TurnTerminalResult.APPLIED:
        await run_in_db(_record_backend_shutdown_marker, thread_id)


async def _shutdown_idle_app_owner(thread_id: str) -> None:
    if await _terminate_app_session(thread_id):
        _CODEX_THREADS.pop(thread_id, None)
        return
    raise RuntimeError(f"{thread_id}: idle app-server session survived shutdown")


async def shutdown_all() -> None:
    pending_cancel: asyncio.CancelledError | None = None
    failures: list[str] = []
    bcast = _default_broadcast
    live_snapshot = tuple(
        (thread_id, _LIVE.get(thread_id)) for thread_id in _LIVE.ids()
    )
    for thread_id, live in live_snapshot:
        if live is None or _LIVE.get(thread_id) is not live:
            continue
        try:
            await _shutdown_live_owner(thread_id, live, bcast)
        except asyncio.CancelledError as exc:
            pending_cancel = pending_cancel or exc
            log.warning(
                "codex_runtime: shutdown cleanup cancelled for %s; retaining "
                "the exact owner",
                thread_id,
            )
        except Exception as exc:
            failures.append(f"{thread_id}: {exc}")
            log.error(
                "codex_runtime: shutdown cleanup failed for %s; retaining "
                "the exact owner: %s",
                thread_id,
                exc,
            )
    for thread_id in list(_APP_SESSIONS):
        if thread_id in _LIVE:
            continue
        try:
            await _shutdown_idle_app_owner(thread_id)
        except asyncio.CancelledError as exc:
            pending_cancel = pending_cancel or exc
            log.warning(
                "codex_runtime: app-session shutdown cancelled for %s; "
                "retaining the exact owner",
                thread_id,
            )
        except Exception as exc:
            failures.append(f"{thread_id}: {exc}")
            log.error(
                "codex_runtime: app-session shutdown failed for %s; retaining "
                "the exact owner: %s",
                thread_id,
                exc,
            )
    remaining_live = _LIVE.ids()
    remaining_app = tuple(_APP_SESSIONS)
    if remaining_live or remaining_app:
        failures.append(
            f"surviving owners: live={remaining_live!r}, app_sessions={remaining_app!r}"
        )
    if pending_cancel is not None:
        raise pending_cancel
    if failures:
        raise RuntimeError("Codex shutdown incomplete: " + "; ".join(failures))
