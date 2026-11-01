"""Codex context-usage proof and exact terminal publication."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from core.stop_causes import PlanLimitStop, frame_cause
from core.turn_core import (
    TerminalSettlement,
    TurnIdentity,
    TurnTerminalResult,
    commit_turn_terminal,
    drive_turn_settlement,
)
import infra.provider_usage as provider_usage
from infra.context_receipts import (
    attach_context_receipt,
    live_context_usage_payload,
    unavailable_context_usage,
)
from infra.db_async import run_in_db
from infra.llm_usage import (
    PlanUsagePersistenceError,
    UsageReceiptConflictError,
)
from plans.public import record_turn_fallback, report_unrecordable_call
from subagents.codex_runtime_state import (
    _LiveCodex,
    context_receipt_identity,
    finalize_live,
    schedule_terminal_drive,
)
from subagents.codex_terminal_context import (  # noqa: F401 - public re-export
    context_usage_with_snapshot,
    live_measured_context_usage,
    provider_context_usage,
    record_provider_context_observation,
)

log = logging.getLogger(__name__)
_TERMINAL_USAGE_UNSET = object()
UNSETTLED_AT_TERMINAL = "Codex Turn ended before its provider write settled"


async def claim_live_terminal(
    live: _LiveCodex,
    outcome: str,
    error: str | BaseException | None = None,
    db=None,
    *,
    identity: TurnIdentity | None = None,
) -> TurnTerminalResult:
    """Quarantine an unsettled write, then terminalize its Turn; its successor drives after the transition."""
    identity = identity or live.corsia_turn
    if identity is None:
        return TurnTerminalResult.CONFLICTING_TERMINAL
    if db is None:
        settlement = await commit_turn_terminal(
            identity.turn_id, outcome, error, quarantine=UNSETTLED_AT_TERMINAL,
        )
        if settlement.thread_id is None:
            return await drive_turn_settlement(settlement)
        schedule_terminal_drive(settlement.thread_id, settlement)
        return settlement.result
    from infra.thread_recovery import ambiguous_stale_writes
    from subagents import codex_runtime

    ambiguous_stale_writes(db, identity.turn_id, error=UNSETTLED_AT_TERMINAL)
    return codex_runtime.persist_turn_terminal(identity.turn_id, outcome, error, db=db)


class CommittedTerminalFailure(Exception):
    """Carry one committed settlement past failed publication."""

    def __init__(
        self, settlement: TerminalSettlement | None, publication_error: BaseException,
    ) -> None:
        super().__init__(str(publication_error))
        self.settlement = settlement
        self.publication_error = publication_error


@contextmanager
def preserve_committed_terminal(terminal) -> Iterator[None]:
    """Carry publication failures that follow an applied terminal commit."""
    try:
        yield
    except BaseException as error:
        if not (
            isinstance(terminal, TerminalSettlement)
            or terminal is TurnTerminalResult.APPLIED
        ):
            raise
        settlement = terminal if isinstance(terminal, TerminalSettlement) else None
        raise CommittedTerminalFailure(settlement, error) from error


async def release_and_drive_terminal(ctx, settlement) -> None:
    """Release the completed live generation before starting its successor."""
    release = finalize_live(ctx.thread_id, ctx.live)
    await drive_turn_settlement(settlement)
    if release is not None:
        await release



async def finish_interrupted_if_still_owned(
    thread_id: str,
    live: _LiveCodex,
    cause: str,
    bcast,
    *,
    db=None,
    defer_drive: bool = False,
    stop_error: PlanLimitStop | None = None,
) -> bool | tuple[bool, TerminalSettlement | None]:
    """Terminalize only while one fenced generation retains the live slot."""
    from subagents import codex_runtime as runtime

    current = runtime._LIVE.get(thread_id)
    if current is None:
        return (True, None) if defer_drive else True
    if current is not live:
        log.error("codex_runtime: %s live owner changed before terminal", thread_id)
        return (False, None) if defer_drive else False
    terminal = await runtime._finish_interrupted_turn(
        thread_id, live, cause, bcast, db, defer_drive=defer_drive, stop_error=stop_error,
    )
    settlement = terminal if isinstance(terminal, TerminalSettlement) else None
    result = settlement.result if settlement is not None else terminal
    stopped = result in {
        TurnTerminalResult.APPLIED,
        TurnTerminalResult.ALREADY_SAME_OUTCOME,
    }
    if not stopped:
        live.provider_context_untrusted = True
    if defer_drive:
        return stopped, settlement if stopped else None
    return stopped


def provider_reported_model_id(live) -> str | None:
    """Return only provider-reported identity; never substitute selection."""
    from subagents.child_thread import resolved_external_model_id

    return resolved_external_model_id(
        "codex",
        getattr(live, "provider_reported_model", None),
    )


async def settle_failed_turn_usage(thread_id: str, live, volume: object) -> dict[str, str]:
    """Settle a failed turn's readings against what it measured.

    As for a completed turn, the ledger writes the delta whole only when
    none of the readings was receipted. A turn that measured nothing -- it
    failed before its first call -- has nothing to settle, and no
    unknown-usage row is invented for it. Returns what the terminal could
    not record, as marker metadata for the failed marker to carry, or ``{}``.
    """
    if volume is None:
        return {}
    return await record_terminal_llm_usage(
        thread_id,
        live,
        volume,
        quiet_if_unmeasured=live.last_assistant_message_id is None,
    )


async def record_terminal_llm_usage(
    thread_id: str, live, volume: object, *, quiet_if_unmeasured: bool = False,
) -> dict[str, str]:
    """Persist what the turn's spend delta still owes the ledger.

    Codex reports a cumulative counter for the turn. Each spent reading is
    recorded as it arrives (``codex_usage_calls.record_call_llm_usage``); the
    terminal hands the ledger the turn's delta and the ledger writes only
    what its receipts do not hold (``plans.usage_calls.record_turn_fallback``),
    so a reading whose receipt failed is still covered, and none is counted
    twice. A turn that recorded no reading writes its whole delta, as before.
    Occupancy is recorded separately from the provider's final input
    measurement.
    """
    from subagents.usage_model import usage_effort, usage_model_id

    model_id = usage_model_id("codex", live)
    measured = (
        volume.ledger_row()
        if isinstance(volume, provider_usage.TokenUsage)
        else {}
    )
    if not measured:
        log.warning("codex_runtime: recording unknown terminal usage for %s", thread_id)
    try:
        if model_id is None:
            # Nothing the ledger can take, but the loss is the Turn's task's
            # defect; without a Turn there is no task to mark.
            log.warning("codex_runtime: no usage row for %s; model is unknown", thread_id)
            turn_id = getattr(live.corsia_turn, "turn_id", None)
            if turn_id is not None:
                await run_in_db(
                    report_unrecordable_call, thread_id, turn_id=turn_id, usage_key="terminal",
                    reason="model_unknown", detail="the provider named no model for this turn",
                )
            return {}
        await run_in_db(
            record_turn_fallback,
            thread_id,
            model_id,
            measured,
            source="external_harness:codex",
            turn_id=getattr(live.corsia_turn, "turn_id", None),
            usage_key="terminal",
            harness=live.harness,
            reasoning_effort=usage_effort(live),
            quiet_if_unmeasured=quiet_if_unmeasured,
        )
    except (PlanUsagePersistenceError, UsageReceiptConflictError) as exc:
        # The entry has already put every failure it could on the Turn's task
        # as a usage_recording_failed defect; what reaches here is the one it
        # could not record. It may not wedge the Turn, so the terminal marker
        # carries it instead of the ledger.
        log.error(
            "codex_runtime: plan-bound usage handoff incomplete for %s; "
            "turn completion continues",
            thread_id,
        )
        return {"usage_accounting_failed": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:
        # Reached only for a Turn bound to no task: nothing durable exists to
        # mark it on but the terminal marker, which carries it.
        log.exception(
            "codex_runtime: failed to persist terminal usage for %s", thread_id
        )
        return {"usage_accounting_failed": f"{type(exc).__name__}: {exc}"}
    return {}


async def attach_context_usage(
    *,
    thread_id: str,
    live,
    message_id: str | None,
    payload: dict,
    context_usage: dict | None,
    db=None,
) -> None:
    """Attach one compact turn-boundary receipt and add its WS projection."""
    context_usage = context_usage_with_snapshot(context_usage, live)
    if not context_usage:
        payload.pop("context_message_id", None)
        return
    receipt = None
    identity = context_receipt_identity(thread_id, live)
    if message_id is not None and identity is not None:
        kwargs = {"context_usage": context_usage, "identity": identity}
        if db is None:
            receipt = await run_in_db(
                attach_context_receipt,
                thread_id,
                message_id,
                **kwargs,
            )
        else:
            receipt = attach_context_receipt(
                thread_id,
                message_id,
                db=db,
                **kwargs,
            )
    if receipt is None:
        payload.pop("context_message_id", None)
    payload["context_usage"] = live_context_usage_payload(context_usage)


def _pop_active_usage(live: _LiveCodex) -> dict | None:
    session = live.session
    pop_usage = getattr(type(session), "pop_active_usage", None)
    if not callable(pop_usage):
        return None
    usage = pop_usage(session)
    return usage if isinstance(usage, dict) else None


def _observe_terminal_context(
    thread_id: str,
    live: _LiveCodex,
    context_usage: dict | None,
    *,
    source: str,
) -> None:
    try:
        record_provider_context_observation(context_usage, live, source=source)
    except Exception as exc:
        log.error(
            "codex_runtime: runtime context observation failed for %s: %s",
            thread_id,
            exc,
        )


async def _attach_terminal_context(
    *,
    thread_id: str,
    live: _LiveCodex,
    message_id: str | None,
    payload: dict,
    context_usage: dict | None,
    db,
) -> None:
    try:
        await attach_context_usage(
            thread_id=thread_id,
            live=live,
            message_id=message_id,
            payload=payload,
            context_usage=context_usage,
            db=db,
        )
    except Exception as exc:
        payload.pop("context_message_id", None)
        log.error(
            "codex_runtime: terminal context receipt failed for %s: %s",
            thread_id,
            exc,
        )


async def publish_claimed_terminal(
    *,
    thread_id: str,
    live: _LiveCodex,
    identity,
    bcast,
    source: str,
    marker_id: str | None = None,
    marker_is_context_owner: bool = False,
    db=None,
    usage: object = _TERMINAL_USAGE_UNSET,
    terminal_type: str = "turn_complete",
    stop_error: object = None,
) -> dict:
    """Publish proof for one terminal-CAS winner and clear Turn-local state."""
    observed_usage = (
        _pop_active_usage(live)
        if usage is _TERMINAL_USAGE_UNSET
        else usage
        if isinstance(usage, dict)
        else None
    )
    context_usage = provider_context_usage(
        observed_usage,
        selected_context_window=live.context_window,
        discovered_context_window=live.discovered_context_window,
    )
    if context_usage is not None:
        live.context_usage_stale_reason = None
    else:
        context_usage = unavailable_context_usage(
            model_context_window=live.context_window,
            discovered_context_window=live.discovered_context_window,
            stale_reason=(
                live.context_usage_stale_reason or "terminal_without_measurement"
            ),
        )
    context_message_id = live.last_assistant_message_id or marker_id
    payload = {
        "type": terminal_type,
        "thread_id": thread_id,
        **identity.frame_fields(), **frame_cause(stop_error),
    }
    if context_message_id is not None:
        payload["context_message_id"] = context_message_id
    if context_usage is not None:
        payload["context_usage"] = live_context_usage_payload(context_usage_with_snapshot(context_usage, live))
    _observe_terminal_context(
        thread_id,
        live,
        context_usage,
        source=source,
    )
    attachment_id = live.last_assistant_message_id
    if attachment_id is None and marker_is_context_owner:
        attachment_id = marker_id
    await _attach_terminal_context(
        thread_id=thread_id,
        live=live,
        message_id=attachment_id,
        payload=payload,
        context_usage=context_usage,
        db=db,
    )
    live.thinking_buf = ""
    live.thinking_provenance = None
    bcast(thread_id, payload)
    return payload
