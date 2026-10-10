"""Own creation and teardown of one preflight-authorized CC process."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from core.checkpoint_wait import CheckpointWaitScope
from core.turn_core import TurnIdentity
from infra.db_async import run_in_db_unless_owned
from subagents.cc_runtime_events import _make_on_event
from subagents.cc_runtime_lifecycle import handle_process_exit
from subagents.cc_runtime_quiescence import cleanup_ephemeral_live, quiesce_live_tools
from subagents.cc_runtime_state import (
    _CLOSE_LIVE_TIMEOUT_S,
    _LIVE,
    _Live,
    activate_bound_turn,
    settle_resume_verdict,
)
from subagents.cc_sessions import resolve_claude_dir
from subagents.cc_startup_context import CCStartupContext
from subagents.cc_startup_session import (
    build_starting_session,
    install_starting_identity,
    restore_prior_identity,
)
from subagents.cc_stream import CCStreamSession

log = logging.getLogger(__name__)


class CCReplacementPredecessorChanged(RuntimeError):
    """A continuing launch lost its exact predecessor before publication."""


def _require_open_process_spawn(live: _Live) -> None:
    if live.checkpoint_wait_scope.stop_requested:
        raise asyncio.CancelledError(
            "Claude Code Stop requested before process spawn"
        )


def _new_live(
    context: CCStartupContext,
    *,
    seed_file: str,
    source_artifacts: list[dict],
    resolved_claude_dir: str,
    continuing_identity: TurnIdentity | None = None,
    continuing_wait_scope: CheckpointWaitScope | None = None,
) -> _Live:
    row = context.row
    reconstruction = context.reconstruction
    live = _Live(
        session=None,
        seed_file=seed_file,
        claude_dir=resolved_claude_dir,
        # ``spawned_by`` is the parent of a delegated child (issue #1617);
        # ``threads.parent_thread_id`` records visible-thread lineage and two of
        # the three subagent creation paths never write it.
        parent_thread_id=row["spawned_by"],
        name=row["title"] or ("subagent" if context.is_subagent else "chat"),
        model=context.model_alias,
        catalog_model_id=row["model"],
        runtime_context_epoch=int(row["runtime_context_epoch"]),
        context_window=context.target_context_window,
        discovered_context_window=context.target_discovered_context_window,
        expected_reported_model=context.expected_reported_model,
        context_snapshot_id=reconstruction.snapshot_id,
        context_snapshot=reconstruction.snapshot.to_dict(),
        source_artifacts=source_artifacts,
        synthetic_resume=context.synthetic_resume,
        tool_definitions=context.tool_definitions,
        prepared_destination_identity=context.launch_identity,
    )
    if continuing_identity is not None:
        if continuing_wait_scope is None:
            raise RuntimeError("CC continuing Turn has no checkpoint wait scope")
        live.corsia_turn = continuing_identity
        live.checkpoint_wait_scope = continuing_wait_scope
        if not activate_bound_turn(live, continuing_identity):
            raise RuntimeError("CC continuing Turn changed during process creation")
    elif continuing_wait_scope is not None:
        raise RuntimeError("CC checkpoint wait scope has no continuing Turn")
    return live


async def _cleanup_failed_launch(
    thread_id: str,
    live: _Live,
    *,
    broadcast: Any,
    finalize_fn: Any,
) -> None:
    live.provider_context_untrusted = True
    cleanup_ephemeral_live(live, thread_id=thread_id)
    if _LIVE.get(thread_id) is not live:
        return
    terminate = getattr(live.session, "terminate", None)
    if terminate is None:
        log.error("CC %s cannot prove startup process death; retaining guard", thread_id)
        return
    try:
        await terminate()
    except BaseException:
        log.exception(
            "CC %s terminate failed during startup cleanup; retaining guard",
            thread_id,
        )
        raise
    finalize_fn(
        thread_id,
        broadcast,
        None,
        expected_live=live,
        emit_closed=False,
    )


async def _handle_process_exit(
    code: int | None,
    stderr: str,
    *,
    thread_id: str,
    context: CCStartupContext,
    live: _Live,
    broadcast: Any,
    surface_exit_error_fn: Any,
    finalize_fn: Any,
) -> None:
    from subagents.cc_runtime_errors import is_resume_not_found

    live.last_exit_stderr = stderr or ""
    missing_resume = bool(
        context.resume_session_id and is_resume_not_found(stderr)
    )
    finish = partial(
        _finish_process_exit, code, stderr, thread_id=thread_id,
        context=context, live=live, broadcast=broadcast,
        surface_exit_error_fn=surface_exit_error_fn, finalize_fn=finalize_fn,
        missing_resume=missing_resume,
    )
    # Claude refused the session before emitting any event, so it never read
    # the input its owning feed wrote; that feed rebuilds and redelivers it.
    owner = live.resume_verdict
    unread = missing_resume and owner is not None and not owner.done()
    if unread or (missing_resume and not live.turn_active and owner is None):
        # With no owner, a rejection before admission waits for the next feed.
        # Once input owns a Turn, the same exit must end it (below); otherwise
        # it leaves that Turn running without a live provider handle.
        live.resume_rejected = True
        await quiesce_live_tools(
            thread_id,
            live,
            live.corsia_turn,
            timeout_s=_CLOSE_LIVE_TIMEOUT_S,
        )
        if not unread or settle_resume_verdict(live, finish):
            return
        if not live.turn_active:
            return
    else:
        # Any other exit (Stop's kill included) is not a rebuildable refusal:
        # release the waiting feed and end the Turn visibly, as before.
        settle_resume_verdict(live, None)
    await finish()


async def _finish_process_exit(
    code: int | None,
    stderr: str,
    *,
    thread_id: str,
    context: CCStartupContext,
    live: _Live,
    broadcast: Any,
    surface_exit_error_fn: Any,
    finalize_fn: Any,
    missing_resume: bool,
) -> None:
    """End the process's Turn visibly; retire a session Claude reported missing."""
    identity = live.corsia_turn
    if missing_resume:
        from infra.db_async import run_in_db
        from subagents.cc_resume_transition import retire_rejected_provider_session

        turn_id = identity.turn_id if identity is not None else "unknown"
        try:
            await run_in_db(
                retire_rejected_provider_session,
                thread_id, context.resume_session_id, None,
            )
        except Exception:
            # A failed retirement must not skip the terminal path below: that
            # would leave this admitted Turn running with no live provider
            # handle, the exact stall this branch exists to end. Retry may
            # resume the still-registered dead session once more; that
            # rejection retires it again on either the feed's rebuild or
            # this path.
            log.exception(
                "cc_runtime: %s failed to retire the missing resume session "
                "for turn %s",
                thread_id, turn_id,
            )
        log.warning(
            "cc_runtime: %s resume rejected after admission; turn %s ends",
            thread_id, turn_id,
        )
    await handle_process_exit(
        thread_id,
        live,
        code,
        stderr,
        broadcast,
        None,
        finish_callback=surface_exit_error_fn,
        finalize_callback=finalize_fn,
    )


def _build_process_session(
    thread_id: str,
    live: _Live,
    context: CCStartupContext,
    *,
    binary: str,
    effort: str | None,
    broadcast: Any,
    surface_exit_error_fn: Any,
    finalize_fn: Any,
) -> Any:
    """Build one provider session with callbacks bound to its exact live."""
    on_event = _make_on_event(live, thread_id, broadcast, None)
    on_exit = partial(
        _handle_process_exit,
        thread_id=thread_id,
        context=context,
        live=live,
        broadcast=broadcast,
        surface_exit_error_fn=surface_exit_error_fn,
        finalize_fn=finalize_fn,
    )
    return build_starting_session(
        thread_id,
        live,
        context,
        binary=binary,
        effort=effort,
        on_event=on_event,
        on_exit=on_exit,
        session_factory=CCStreamSession,
    )


def _require_continuing_predecessor(
    thread_id: str,
    expected_live: _Live | None,
    continuing_identity: TurnIdentity | None,
    continuing_wait_scope: CheckpointWaitScope | None,
) -> None:
    """Validate one uninterrupted check-and-publish replacement boundary."""
    if continuing_identity is None:
        if expected_live is not None:
            raise RuntimeError("CC replacement predecessor has no continuing Turn")
        return
    if expected_live is None:
        raise RuntimeError("CC continuing Turn has no expected predecessor")
    if (
        _LIVE.get(thread_id) is not expected_live
        or not expected_live.turn_active
        or expected_live.corsia_turn != continuing_identity
        or expected_live.checkpoint_wait_scope is not continuing_wait_scope
    ):
        raise CCReplacementPredecessorChanged(
            "Claude Code checkpoint predecessor changed before replacement publication"
        )


def _require_publishable(
    thread_id: str,
    held_lock: asyncio.Lock,
    expected_live: _Live | None,
    continuing_identity: TurnIdentity | None,
    continuing_wait_scope: CheckpointWaitScope | None,
) -> None:
    _LIVE.require_held(thread_id, held_lock)
    _require_continuing_predecessor(
        thread_id, expected_live, continuing_identity, continuing_wait_scope,
    )


async def _install_identity_off_loop(
    thread_id: str, context: CCStartupContext, session: Any, db: Any,
    landed: list[str | None],
) -> None:
    """Register the prepared identity on the DB pool, recording it in ``landed`` on the worker.

    A launch cancelled mid-write still joins the worker, so ``landed`` tells its
    cleanup whether there is a registration to undo.
    """
    def install() -> None:
        landed.append(install_starting_identity(thread_id, context, session, db))

    await run_in_db_unless_owned(db, install)


def _log_started_process(thread_id: str, context: CCStartupContext) -> None:
    log.info(
        "%s CC %s=%s (cc_session=%s)",
        "Resumed" if context.resume_session_id else "Cold-started",
        "subagent" if context.is_subagent else "main",
        thread_id,
        context.cc_session_id,
    )


async def launch_cc_process(
    thread_id: str,
    context: CCStartupContext,
    *,
    binary: str,
    claude_dir: str,
    effort: str | None,
    broadcast: Any,
    seed_file: str,
    source_artifacts: list[dict],
    surface_exit_error_fn: Any,
    finalize_fn: Any,
    db: Any,
    held_lock: asyncio.Lock,
    expected_live: _Live | None = None,
    continuing_identity: TurnIdentity | None = None,
    continuing_wait_scope: CheckpointWaitScope | None = None,
) -> _Live:
    """Register and start one exact CC process under the live-handle owner."""
    resolved_claude_dir = resolve_claude_dir(claude_dir)
    live = _new_live(
        context,
        seed_file=seed_file,
        source_artifacts=source_artifacts,
        resolved_claude_dir=resolved_claude_dir,
        continuing_identity=continuing_identity,
        continuing_wait_scope=continuing_wait_scope,
    )
    landed: list[str | None] = []  # the predecessor id, once the registration committed

    try:
        _require_open_process_spawn(live)
        from subagents.cc_runtime_helpers import _validate_working_dir

        _validate_working_dir(context.working_dir, context.row["project_id"])
        session = _build_process_session(
            thread_id,
            live,
            context,
            binary=binary,
            effort=effort,
            broadcast=broadcast,
            surface_exit_error_fn=surface_exit_error_fn,
            finalize_fn=finalize_fn,
        )
        live.session = session
        # Fail before writing when the live handle or predecessor is already stale.
        _require_publishable(
            thread_id, held_lock, expected_live, continuing_identity, continuing_wait_scope,
        )
        await _install_identity_off_loop(thread_id, context, session, db, landed)
        # The write yielded the loop. The held lock keeps another launch for this
        # thread out; the checks run again with no await between them and
        # publication, so check-and-publish stays one step.
        _require_publishable(
            thread_id, held_lock, expected_live, continuing_identity, continuing_wait_scope,
        )
        _LIVE.publish(thread_id, live, held=held_lock)
        await live.session.start()
        _log_started_process(thread_id, context)
        return live
    except BaseException:
        try:
            await _cleanup_failed_launch(
                thread_id,
                live,
                broadcast=broadcast,
                finalize_fn=finalize_fn,
            )
        finally:
            if landed and _LIVE.get(thread_id) is not live:
                await run_in_db_unless_owned(
                    db, restore_prior_identity, thread_id, context, landed[0], db,
                )
        raise
