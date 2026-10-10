"""Causally bind one prepared Claude Code frame to its durable Turn."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.checkpoint_wait import CheckpointWaitScope
from core.turn_core import TerminalSettlement as TurnSettlement, TurnIdentity, TurnTerminalResult
from core.turn_core import (
    bind_preadmitted_turn, persist_turn_start_bound, terminal_error_detail,
)
from core.turn_activity import turn_activity_frame
from subagents import cc_trust
from subagents.cc_process_exit import _CommittedTerminalFailure, claim_terminal
from subagents.cc_event_context_usage import (
    consume_and_attach_terminal_context_usage,
)
from subagents.cc_runtime_state import Broadcast, _Live, activate_bound_turn

log = logging.getLogger("subagents.cc_runtime")
TerminalSettlement = Callable[[str, str | BaseException | None], Awaitable[None]]
BeforeProviderWrite = Callable[
    [_Live, TurnIdentity], Awaitable[tuple[_Live, Any]]
]

_UNVERIFIED_INPUT_DELIVERY_DETAIL = (
    "Claude Code input delivery could not be verified; provider output was discarded."
)


def require_owned_accept_db(db) -> None:
    if db is not None:
        raise RuntimeError("CC async acceptance requires an owned DB connection")


async def _broadcast_turn_activity(
    thread_id: str,
    identity: TurnIdentity,
    bcast: Broadcast,
) -> None:
    """Announce one admitted CC Turn, attributed to its visible root."""
    bcast(thread_id, await turn_activity_frame(thread_id, identity))


def _source_artifact(*, method: str, snapshot_id: str | None, payload: dict) -> dict:
    from core.effective_context import provider_source_artifact

    return provider_source_artifact(
        harness="claude-code", method=method,
        snapshot_id=snapshot_id, payload=payload,
    )


def _transcript_artifact(live: _Live, snapshot_id: str | None) -> dict | None:
    transcript_path = getattr(live.session, "reconstructed_transcript_path", None)
    if not transcript_path:
        return None
    try:
        transcript_text = Path(transcript_path).read_text(encoding="utf-8")
    except OSError:
        log.warning("CC reconstructed transcript unreadable: %s", transcript_path)
        return None
    return _source_artifact(
        method="cc-reconstructed-transcript", snapshot_id=snapshot_id,
        payload={
            "path": transcript_path,
            "cc_session_id": (
                getattr(live.session, "resume_session_id", None)
                or getattr(live.session, "session_id", "")
            ),
            "content_hash": hashlib.sha256(transcript_text.encode("utf-8")).hexdigest(),
            "line_count": len(transcript_text.splitlines()),
        },
    )


def _bind_turn(live: _Live, frame, identity: TurnIdentity) -> None:
    if live.corsia_turn != identity:
        live.checkpoint_wait_scope = CheckpointWaitScope()
        live.turn_active = False
        live.turn_terminal = None
        live.interrupt_requested = False
        live.apply_now_interrupt_turn = None
    live.last_assistant_message_id = None
    live.last_call_usage = None
    live.turn_usage = None
    live.unreceipted_calls = 0
    live.reasoning_provenance = None
    live.reasoning_provider_message_id = None
    live.tool_execution_admitted = False
    live.claimed_tool_calls.clear()
    live.corsia_turn = identity


async def _admit_turn(
    thread_id: str,
    live: _Live,
    frame,
    turn_id: str,
    preadmitted_turn: TurnIdentity | None,
) -> TurnIdentity:
    def bind(identity: TurnIdentity) -> None:
        _bind_turn(live, frame, identity)

    def unbind(identity: TurnIdentity) -> None:
        if live.corsia_turn == identity:
            live.corsia_turn = None

    if preadmitted_turn is not None:
        if preadmitted_turn.turn_id != turn_id:
            raise ValueError("preadmitted Claude Code Turn does not match turn_id")
        await bind_preadmitted_turn(thread_id, preadmitted_turn, bind, unbind)
        if not activate_bound_turn(live, preadmitted_turn):
            raise RuntimeError("Claude Code Turn changed during admission")
        return preadmitted_turn

    identity = await persist_turn_start_bound(
        turn_id, thread_id, live.model or "claude-code", frame.turn_text,
        bind, unbind,
    )
    if not activate_bound_turn(live, identity):
        raise RuntimeError("Claude Code Turn changed during admission")
    return identity


def _prepare_live_turn(live: _Live, frame) -> None:
    live.context_snapshot_id = frame.snapshot_id
    live.context_snapshot = frame.snapshot.to_dict()
    live.continuity_accounting = frame.continuity_accounting
    transcript = _transcript_artifact(live, frame.snapshot_id)
    artifacts = [
        *([transcript] if transcript else []),
        _source_artifact(
            method="cc-user-frame", snapshot_id=frame.snapshot_id,
            payload={"frame_text": frame.turn_text, "delivery_mode": "turn-only"},
        ),
    ]
    live.source_artifacts = artifacts
    identity = live.corsia_turn
    if not isinstance(identity, TurnIdentity) or not activate_bound_turn(
        live, identity,
    ):
        raise RuntimeError("Claude Code handle changed before provider write")


@dataclass
class _DeliveryObservation:
    mark_accepted: Callable[[], None] | None
    write_observed: bool = False
    accepted: bool = False
    unverified: bool = False

    def observe_write(self) -> None:
        self.write_observed = True

    def accept(self) -> None:
        self.accepted = True
        if self.mark_accepted is not None:
            self.mark_accepted()

    def distrust(self, live: _Live) -> None:
        self.unverified = True
        cc_trust.distrust(live)
        live.input_delivery_unproven = True


async def _settle_input_delivery(
    settle_delivery: Callable[[], Awaitable[None]],
) -> None:
    settlement = asyncio.ensure_future(settle_delivery())
    while not settlement.done():
        try:
            await asyncio.shield(settlement)
        except asyncio.CancelledError:
            continue
    settlement.result()


async def _feed_frame(
    thread_id: str,
    live: _Live,
    frame,
    observation: _DeliveryObservation,
    *,
    settle_delivery: Callable[[], Awaitable[None]] | None,
    settle_delivery_terminal: TerminalSettlement | None,
    identity: TurnIdentity,
    bcast: Broadcast,
    media_projection,
    input_message_id: str | None = None,
) -> None:
    async with live.event_lock:
        try:
            if live.checkpoint_wait_scope.stop_requested:
                raise asyncio.CancelledError(
                    "Claude Code Stop requested before provider write"
                )
            feed_kwargs: dict[str, Any] = (
                {"media_projection": media_projection}
                if media_projection is not None else {}
            )
            if input_message_id is not None:  # the anchor row, as the frame uuid
                feed_kwargs["input_id"] = input_message_id
            await live.session.feed(
                frame.turn_text,
                on_write_observed=observation.observe_write,
                on_accepted=observation.accept,
                **feed_kwargs,
            )
            if not observation.accepted:
                if observation.write_observed:
                    observation.distrust(live)
                raise RuntimeError("Claude Code did not acknowledge input delivery")
        except BaseException as error:
            if not observation.write_observed:
                stopped = live.checkpoint_wait_scope.stop_requested
                if settle_delivery_terminal is not None:
                    outcome = "cancelled" if stopped or isinstance(error, asyncio.CancelledError) else "error"
                    await settle_delivery_terminal(outcome, terminal_error_detail(error) or None)
                if not stopped:
                    await _report_prewrite_failure(thread_id, live, identity, error, bcast)
            if observation.write_observed and (
                not observation.accepted or settle_delivery is not None
            ):
                observation.distrust(live)
            raise
        if settle_delivery is None:
            return
        try:
            await _settle_input_delivery(settle_delivery)
        except BaseException:
            observation.distrust(live)
            raise


async def finish_unverified_input_delivery(
    thread_id: str,
    live: _Live,
    identity: TurnIdentity,
    *,
    bcast: Broadcast,
    db=None,
) -> TurnSettlement:
    """Close one exact turn whose triggering input lacks durable proof."""
    result = await claim_terminal(
        live, "error", _UNVERIFIED_INPUT_DELIVERY_DETAIL,
        db=db, identity=identity, defer_drive=True,
    )
    assert isinstance(result, TurnSettlement)
    if result.result is not TurnTerminalResult.APPLIED:
        return result
    barrier = live.turn_terminal
    payload = {
        "type": "turn_complete", "thread_id": thread_id,
        **identity.frame_fields(),
    }
    try:
        await consume_and_attach_terminal_context_usage(
            live=live, child_id=thread_id, payload=payload,
            message_id=None, db=db,
        )
        bcast(thread_id, payload)
    except BaseException as error:
        raise _CommittedTerminalFailure(result, error) from error
    finally:
        if live.corsia_turn == identity:
            live.turn_active = False
        if barrier is not None:
            barrier.set()
    return result


async def _report_prewrite_failure(
    thread_id: str,
    live: _Live,
    identity: TurnIdentity,
    error: BaseException,
    bcast: Broadcast,
) -> None:
    """Commit a failed admission and return its successor work past the lock."""
    outcome = "cancelled" if isinstance(error, asyncio.CancelledError) else "error"
    barrier = live.turn_terminal
    terminal = await claim_terminal(
        live, outcome, str(error), identity=identity, defer_drive=True,
    )
    assert isinstance(terminal, TurnSettlement)
    try:
        if terminal.result is TurnTerminalResult.APPLIED:
            payload = {
                "type": "turn_complete", "thread_id": thread_id,
                **identity.frame_fields(),
            }
            await consume_and_attach_terminal_context_usage(
                live=live, child_id=thread_id,
                payload=payload, message_id=None,
            )
            bcast(thread_id, payload)
    except BaseException as publication_error:
        raise _CommittedTerminalFailure(terminal, publication_error) from error
    finally:
        if live.corsia_turn == identity:
            live.turn_active = False
        if barrier is not None:
            barrier.set()
    raise _CommittedTerminalFailure(terminal, error) from error


async def accept_reconstructed_input(
    thread_id: str,
    live: _Live,
    frame_reconstruction,
    *,
    turn_id: str,
    bcast: Broadcast,
    db=None,
    mark_accepted: Callable[[], None] | None = None,
    mark_ambiguous: Callable[[], None] | None = None,
    settle_delivery: Callable[[], Awaitable[None]] | None = None,
    transfer_delivery_to_turn: Callable[[], None] | None = None,
    settle_delivery_terminal: TerminalSettlement | None = None,
    preadmitted_turn: TurnIdentity | None = None,
    media_projection=None,
    before_provider_write: BeforeProviderWrite | None = None,
    input_message_id: str | None = None,
) -> TurnIdentity:
    """Feed one prepared frame and bind all output behind its durable input."""
    require_owned_accept_db(db)
    identity = await _admit_turn(
        thread_id, live, frame_reconstruction, turn_id, preadmitted_turn)
    if transfer_delivery_to_turn is not None:
        transfer_delivery_to_turn()
    observation = _DeliveryObservation(mark_accepted)
    boundary_live = live
    try:
        await _broadcast_turn_activity(thread_id, identity, bcast)
        if before_provider_write is not None:
            live, frame_reconstruction = await before_provider_write(live, identity)
            if (
                boundary_live.checkpoint_wait_scope.stop_requested
                or live.checkpoint_wait_scope.stop_requested
            ):
                if not activate_bound_turn(live, identity):
                    raise RuntimeError(
                        "Claude Code Stop boundary returned a different Turn"
                    )
                raise asyncio.CancelledError
        if live.checkpoint_wait_scope.stop_requested:
            raise asyncio.CancelledError("Claude Code Stop requested before provider write")
        _prepare_live_turn(live, frame_reconstruction)
    except BaseException as exc:
        stopped = (
            boundary_live.checkpoint_wait_scope.stop_requested
            or live.checkpoint_wait_scope.stop_requested
        )
        if settle_delivery_terminal is not None:
            outcome = "cancelled" if stopped or isinstance(exc, asyncio.CancelledError) else "error"
            await settle_delivery_terminal(outcome, terminal_error_detail(exc) or None)
        if not stopped:
            await _report_prewrite_failure(thread_id, live, identity, exc, bcast)
        raise
    try:
        await _feed_frame(
            thread_id, live, frame_reconstruction, observation,
            settle_delivery=settle_delivery,
            settle_delivery_terminal=settle_delivery_terminal,
            identity=identity, bcast=bcast,
            media_projection=media_projection, input_message_id=input_message_id)
    except _CommittedTerminalFailure:
        raise
    except BaseException as exc:
        if not observation.write_observed:
            raise
        cc_trust.distrust(live)
        observation.unverified = observation.unverified or not observation.accepted
        if not observation.accepted and mark_ambiguous is not None:
            mark_ambiguous()
        if observation.unverified:
            live.input_delivery_unproven = True
            if settle_delivery is not None:
                await _settle_input_delivery(settle_delivery)
            terminal = await finish_unverified_input_delivery(
                thread_id, live, identity, bcast=bcast, db=db,
            )
            raise _CommittedTerminalFailure(terminal, exc) from exc
        raise
    return identity
