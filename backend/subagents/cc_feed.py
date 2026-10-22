"""Single Claude Code input-acceptance path for main and delegated threads."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from core.turn_core import TurnIdentity, drive_turn_settlement
from llm.resolution import ExecutionTarget
from subagents.cc_accept import (
    TerminalSettlement,
    accept_reconstructed_input,
    require_owned_accept_db,
)
from subagents.cc_destination import build_cc_user_frame
from subagents.cc_feed_transition import CCFeedTransition, replace_if_required
from subagents.cc_runtime_errors import CCLiveTurnBusy
from subagents.cc_runtime_helpers import _default_broadcast
from subagents.cc_runtime_state import Broadcast, _LIVE, _Live
from subagents.cc_process_exit import _CommittedTerminalFailure
from subagents.child_thread import record_user_frame

if TYPE_CHECKING:
    from core.checkpoint_wait import CheckpointWaitScope
    from core.context_payload import PreparedContextPayload
    from core.media_projection import ValidatedMediaProjection


@dataclass
class _FeedAcceptance:
    turn_id: str
    record: bool
    edited: bool
    agent_meta: dict | None
    persist_input_once: Callable[[], Awaitable[None]] | None
    mark_accepted: Callable[[], None] | None
    mark_ambiguous: Callable[[], None] | None
    settle_delivery: Callable[[], Awaitable[None]] | None
    transfer_delivery_to_turn: Callable[[], None] | None
    settle_delivery_terminal: TerminalSettlement | None
    preadmitted_turn: TurnIdentity | None
    media_projection: ValidatedMediaProjection | None
    input_message_id: str | None  # the anchor row, sent as the frame uuid


async def _settle_terminal_boundary(live: _Live) -> None:
    pending = live.turn_terminal
    if pending is None or live.turn_active:
        return
    await pending.wait()
    if live.turn_terminal is pending and not live.turn_active:
        live.turn_terminal = None


async def _maintain_initial_boundary(
    transition: CCFeedTransition,
    prepared_context,
    frame,
    current: _Live,
    _identity: TurnIdentity,
):
    from subagents.cc_replay_authority import require_cc_provider_fit
    from core.fork_cc_context import CCForkContext
    from subagents.cc_resume_transition import (
        replace_same_turn_after_checkpoint_locked,
    )
    from subagents.harness_checkpoint_resume import (
        maintain_external_initial_boundary,
    )

    if current.resume_rejected:
        replacement, replacement_frame, _ = (
            await transition.rebuild_rejected_resume(current)
        )
        return replacement, replacement_frame
    if transition.refresh_callback is None:
        require_cc_provider_fit(prepared_context, transition.target)
        current.session.tool_context = CCForkContext(
            transition.target, prepared_context.fit_ledger.rendered_input_tokens,
        )
        return current, frame
    adopted = await maintain_external_initial_boundary(
        current,
        transition.thread_id,
        "claude-code",
        prepared_context,
        transition.refresh_current_context,
    )
    if adopted is None:
        require_cc_provider_fit(prepared_context, transition.target)
        current.session.tool_context = CCForkContext(
            transition.target, prepared_context.fit_ledger.rendered_input_tokens,
        )
        return current, frame
    require_cc_provider_fit(adopted, transition.target)
    replacement, replacement_frame, _ = (
        await replace_same_turn_after_checkpoint_locked(
            transition.thread_id,
            current,
            adopted,
            transition.text,
            is_subagent=transition.is_subagent,
            held_lock=transition.held_lock,
        )
    )
    return replacement, replacement_frame


async def _record_and_accept(
    transition: CCFeedTransition,
    live: _Live,
    acceptance: _FeedAcceptance,
) -> TurnIdentity | None:
    prepared_context = transition.prepared_context
    expected_model = transition.target.expected_reported_model
    if expected_model is not None:
        live.expected_reported_model = expected_model
    live.catalog_model_id = transition.target.catalog_id
    if prepared_context is not None:
        live.runtime_context_epoch = prepared_context.runtime_context_epoch
    live.bind_windows(transition.target)
    live.user_driving = True
    frame = build_cc_user_frame(
        thread_id=transition.thread_id,
        text=transition.text,
        live=live,
        target=transition.target,
        prepared_context=prepared_context,
        db=transition.db,
    )

    if acceptance.persist_input_once is not None:
        await acceptance.persist_input_once()
    elif acceptance.record:
        record_user_frame(
            transition.thread_id,
            transition.text,
            db=transition.db,
            edited=acceptance.edited,
            extra_meta=acceptance.agent_meta,
            turn_id=acceptance.turn_id,
        )
    return await accept_reconstructed_input(
        transition.thread_id,
        live,
        frame,
        turn_id=acceptance.turn_id,
        mark_accepted=acceptance.mark_accepted,
        mark_ambiguous=acceptance.mark_ambiguous,
        settle_delivery=acceptance.settle_delivery,
        transfer_delivery_to_turn=acceptance.transfer_delivery_to_turn,
        settle_delivery_terminal=acceptance.settle_delivery_terminal,
        preadmitted_turn=acceptance.preadmitted_turn,
        media_projection=acceptance.media_projection,
        input_message_id=acceptance.input_message_id,
        before_provider_write=partial(
            _maintain_initial_boundary,
            transition,
            prepared_context,
            frame,
        ),
        bcast=transition.bcast,
    )


def _awaits_resume_verdict(live: _Live) -> bool:
    """Whether Claude may still refuse this native resume after the write."""
    return bool(
        getattr(live.session, "resume_session_id", None)
        and not live.synthetic_resume
        and not live.initialized
        and not live.resume_rejected
    )


_DeferredFinish = Callable[[], Awaitable[None]]


def _wait_scope(live: _Live) -> CheckpointWaitScope:
    return cast("CheckpointWaitScope", live.checkpoint_wait_scope)


async def _await_resume_verdict(
    live: _Live, verdict: asyncio.Future,
) -> _DeferredFinish | None:
    """Return Claude's verdict, or None once Stop addresses this Turn.

    Stop's kill suppresses the exit callback and then needs this admission
    lock to close the Turn, so Stop must release the wait itself.
    """
    # Holds only the caller's in-memory admission lock: the feed owns no DB
    # connection (require_owned_accept_db) and no transaction is open here.
    # Cancellation propagates: the caller clears resume_verdict, so a later
    # refusal finds no owner and ends the Turn visibly (#2412's path).
    stop = asyncio.ensure_future(_wait_scope(live).wait_for_stop())
    try:
        await asyncio.wait((verdict, stop), return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        # A refusal already handed its terminal here; end that Turn visibly.
        if verdict.done() and verdict.result() is not None:
            await asyncio.shield(verdict.result()())
        raise
    finally:
        stop.cancel()
    return cast("_DeferredFinish | None", verdict.result() if verdict.done() else None)


async def _redeliver_rejected_resume(
    transition: CCFeedTransition,
    live: _Live,
    identity: TurnIdentity,
    finish: _DeferredFinish,
    acceptance: _FeedAcceptance,
) -> TurnIdentity:
    """Deliver the same input in the same Turn after Claude refused it unread."""
    if _wait_scope(live).stop_requested:
        await finish()
        return identity
    try:
        replacement, frame, identity = await transition.rebuild_rejected_resume(live)
    except BaseException:
        if _LIVE.get(transition.thread_id) is live and live.corsia_turn == identity:
            await finish()
        raise
    return await accept_reconstructed_input(
        transition.thread_id, replacement, frame, turn_id=identity.turn_id,
        bcast=transition.bcast, preadmitted_turn=identity,
        input_message_id=acceptance.input_message_id,
    )


async def _accept_confirming_resume(
    transition: CCFeedTransition,
    live: _Live,
    acceptance: _FeedAcceptance,
) -> TurnIdentity | None:
    """Accept input; own Claude's verdict on a native resume until it speaks."""
    if not _awaits_resume_verdict(live):
        return await _record_and_accept(transition, live, acceptance)
    verdict = asyncio.get_running_loop().create_future()
    live.resume_verdict = verdict
    waited = False
    try:
        identity = await _record_and_accept(transition, live, acceptance)
        if identity is None or _LIVE.get(transition.thread_id) is not live:
            return identity
        waited = True
        finish = await _await_resume_verdict(live, verdict)
    finally:
        live.resume_verdict = None
        # A refusal handed over while accepting has no rebuild; end it here.
        if not waited and verdict.done() and verdict.result() is not None:
            await asyncio.shield(verdict.result()())
    if finish is None:
        return identity
    return await _redeliver_rejected_resume(transition, live, identity, finish, acceptance)


async def _accept_under_lock(
    transition: CCFeedTransition,
    acceptance: _FeedAcceptance,
) -> TurnIdentity | None:
    live = _LIVE.get(transition.thread_id)
    if live is None:
        live = await transition.resume()
        if live is None:
            return None
    live = await transition.require_idle(live)
    if live is None:
        return None
    transition.require_direct_fit()
    live = await transition.replace_quarantined(live)
    if live is None:
        return None
    await _settle_terminal_boundary(live)
    live = await replace_if_required(transition, live)
    if live is None:
        return None
    await transition.refresh_once()
    live = await transition.consume_cold_transition(live)
    if live is None:
        return None
    return await _accept_confirming_resume(transition, live, acceptance)


async def ensure_live_then_feed(
    thread_id: str, text: str, *, target: ExecutionTarget, turn_id: str,
    effort: str | None = None, claude_dir: str | None = None,
    binary: str | None = None, broadcast: Broadcast | None = None,
    is_subagent: bool | None = None, record: bool = True,
    edited: bool = False, db=None, agent_meta: dict | None = None,
    persist_input_once: Callable[[], Awaitable[None]] | None = None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: TerminalSettlement | None = None,
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: Callable[[], Awaitable[PreparedContextPayload]] | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection: ValidatedMediaProjection | None = None,
    input_message_id: str | None = None,
) -> TurnIdentity | None:
    """Ensure one live CC handle, then accept input under its transition lock."""
    require_owned_accept_db(db)
    if target.harness != "claude-code" or not target.transport_model:
        raise ValueError("CC feed received a non-Claude Code target")
    transition = CCFeedTransition(
        thread_id=thread_id, text=text, target=target, effort=effort,
        claude_dir=claude_dir, binary=binary, broadcast=broadcast,
        is_subagent=is_subagent, bcast=broadcast or _default_broadcast,
        db=db, prepared_context=prepared_context,
        refresh_callback=refresh_prepared_context,
        held_lock=_LIVE.lock(thread_id),
    )
    acceptance = _FeedAcceptance(
        turn_id=turn_id, record=record, edited=edited, agent_meta=agent_meta,
        persist_input_once=persist_input_once, mark_accepted=mark_accepted,
        mark_ambiguous=mark_ambiguous, settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        preadmitted_turn=preadmitted_turn, media_projection=media_projection,
        input_message_id=input_message_id,
    )
    try:
        async with transition.held_lock:
            return await _accept_under_lock(transition, acceptance)
    except _CommittedTerminalFailure as failure:
        await drive_turn_settlement(failure.settlement)
        raise failure.publication_error


async def feed(
    child_thread_id: str, text: str, *, target: ExecutionTarget, turn_id: str,
    effort: str | None = None, broadcast: Broadcast | None = None,
    record: bool = True, edited: bool = False, db=None,
    agent_meta: dict | None = None,
    persist_input_once: Callable[[], Awaitable[None]] | None = None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: TerminalSettlement | None = None,
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: Callable[[], Awaitable[PreparedContextPayload]] | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection: ValidatedMediaProjection | None = None,
    input_message_id: str | None = None,
) -> TurnIdentity | None:
    """Feed text into a delegated CC thread."""
    return await ensure_live_then_feed(
        child_thread_id, text, target=target, turn_id=turn_id, effort=effort,
        is_subagent=True, broadcast=broadcast, record=record, edited=edited,
        db=db, agent_meta=agent_meta, persist_input_once=persist_input_once,
        mark_accepted=mark_accepted, mark_ambiguous=mark_ambiguous,
        settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
        preadmitted_turn=preadmitted_turn, media_projection=media_projection,
        input_message_id=input_message_id,
    )


async def feed_main(
    thread_id: str, text: str, *, target: ExecutionTarget, turn_id: str,
    effort: str | None = None, project_id: str | None = None,
    binary: str | None = None, claude_dir: str | None = None,
    broadcast: Broadcast | None = None, db=None,
    persist_input_once: Callable[[], Awaitable[None]] | None = None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: TerminalSettlement | None = None,
    prepared_context: PreparedContextPayload | None = None,
    refresh_prepared_context: Callable[[], Awaitable[PreparedContextPayload]] | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection: ValidatedMediaProjection | None = None,
    input_message_id: str | None = None,
) -> TurnIdentity | None:
    """Feed text into the main Claude Code process, starting it if needed."""
    return await ensure_live_then_feed(
        thread_id, text, target=target, effort=effort, turn_id=turn_id,
        claude_dir=claude_dir, binary=binary, broadcast=broadcast,
        is_subagent=False, record=False, db=db,
        persist_input_once=persist_input_once, mark_accepted=mark_accepted,
        mark_ambiguous=mark_ambiguous, settle_delivery=settle_delivery,
        transfer_delivery_to_turn=transfer_delivery_to_turn,
        settle_delivery_terminal=settle_delivery_terminal,
        prepared_context=prepared_context,
        refresh_prepared_context=refresh_prepared_context,
        preadmitted_turn=preadmitted_turn, media_projection=media_projection,
        input_message_id=input_message_id,
    )
