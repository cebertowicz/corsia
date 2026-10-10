"""Claude Code terminal token-volume and retarget-continuity accounting."""

from __future__ import annotations

import logging

import infra.provider_usage as provider_usage
from infra.db_async import run_in_db
from infra.llm_usage import (
    PlanUsagePersistenceError,
    UsageReceiptConflictError,
)
from plans.public import record_observed_call_usage, record_turn_fallback, report_unrecordable_call
import subagents.cc_trust as cc_trust
from subagents.cc_runtime_state import _Live
from subagents.child_thread import resolved_external_model_id
from subagents.usage_model import usage_effort, usage_model_id

log = logging.getLogger("subagents.cc_runtime_events")

_CONTINUITY_MIN_FLOOR_TOKENS = 8_192
_CONTINUITY_MIN_RETAINED_FRACTION = 0.10


def provider_reported_model_id(live: _Live) -> str | None:
    """Return only provider-reported identity; never substitute selection."""
    return resolved_external_model_id("cc", live.provider_reported_model)


def _assert_post_retarget_continuity(
    thread_id: str,
    live: _Live,
    *,
    expected_floor: int | float | None,
    effective_input_tokens: int | None,
) -> None:
    if (
        isinstance(expected_floor, bool)
        or not isinstance(expected_floor, (int, float))
        or expected_floor < _CONTINUITY_MIN_FLOOR_TOKENS
    ):
        return
    if effective_input_tokens is None:
        cc_trust.distrust(live)
        log.error(
            "cc_runtime: post-retarget continuity assertion unavailable for %s; "
            "forcing cold reconstruction on the next turn (expected_floor=%s)",
            thread_id, expected_floor,
        )
        return
    retained_fraction = effective_input_tokens / expected_floor
    if retained_fraction >= _CONTINUITY_MIN_RETAINED_FRACTION:
        return
    cc_trust.distrust(live)
    log.error(
        "cc_runtime: post-retarget continuity collapse for %s; effective_input=%s "
        "is %.1f%% of expected_floor=%s (minimum=%.1f%%); forcing cold "
        "reconstruction on the next turn",
        thread_id, effective_input_tokens, retained_fraction * 100,
        expected_floor, _CONTINUITY_MIN_RETAINED_FRACTION * 100,
    )


def _consume_turn_volume(
    live: _Live, usage: object,
) -> provider_usage.TokenUsage | None:
    volume = live.turn_usage
    live.turn_usage = None
    if volume is not None:
        return volume
    return provider_usage.from_anthropic(usage) if isinstance(usage, dict) else None


def _assert_terminal_occupancy(
    thread_id: str, live: _Live, usage: object,
) -> None:
    expected_floor = live.post_retarget_context_usage_floor
    live.post_retarget_context_usage_floor = None
    occupancy = (
        provider_usage.from_anthropic(usage) if isinstance(usage, dict) else None
    )
    if occupancy is None:
        _assert_post_retarget_continuity(
            thread_id, live, expected_floor=expected_floor,
            effective_input_tokens=None,
        )
        log.warning(
            "cc_runtime: dropped terminal usage measurement for %s: "
            "unusable occupancy sample (%s)",
            thread_id, type(usage).__name__,
        )
        return
    _assert_post_retarget_continuity(
        thread_id, live, expected_floor=expected_floor,
        effective_input_tokens=occupancy.input_total,
    )


async def record_call_llm_usage(
    thread_id: str, live: _Live, *, message_id: str, usage: object, model: object = None,
) -> None:
    """Persist one model call's usage as its own receipt, keyed by its message id.

    Every call the provider measured is receipted under the message id the
    provider gave it, so a Turn's partial success is represented as receipts
    plus per-call defects, never reconstructed from a total. A Turn may span
    several calls; each is recorded as it arrives, durable before the Turn
    ends and idempotent by its key, so a replayed event is the same receipt
    and a restart finds what was already written. A receipt the entry could
    not write is a recorded defect there; a failure the entry cannot even
    record escapes, as any failed write does. The receipt names the model
    the provider reported for that call; only a call that named none falls
    back to the session's reported model. The canonical ledger marks
    subscription usage cost as unknown, as for the Turn total.
    """
    usage_key = f"message:{message_id}"
    measured = provider_usage.from_anthropic(usage) if isinstance(usage, dict) else None
    model_id = (
        resolved_external_model_id("cc", model) if isinstance(model, str) else None
    ) or usage_model_id("cc", live)
    if measured is None or model_id is None:
        reason = "measurement_missing" if measured is None else "model_unknown"
        log.warning(
            "cc_runtime: no per-call usage row for %s (%s): %s", thread_id, usage_key, reason,
        )
        # Nothing the ledger can take, but the loss is the Turn's task's
        # defect; without a Turn there is no task to mark.
        turn_id = getattr(live.corsia_turn, "turn_id", None)
        if turn_id is not None:
            await run_in_db(
                report_unrecordable_call, thread_id, turn_id=turn_id, usage_key=usage_key,
                reason=reason, detail="the provider named no model or measurement for this call",
            )
        return
    await run_in_db(
        record_observed_call_usage,
        thread_id,
        model_id,
        measured.ledger_row(),
        source="external_harness:claude-code",
        turn_id=getattr(live.corsia_turn, "turn_id", None),
        usage_key=usage_key,
        harness=live.harness,
        reasoning_effort=usage_effort(live),
    )


async def report_incomplete_call_usage(thread_id: str, live: _Live) -> None:
    """Record a call whose measured identity never reached ``final_call_usage``.

    Distinct from ``measurement_missing`` (the provider measured nothing for
    the call) and from an id-less call receipted by ordinal (the provider
    named no id but its usage is otherwise trustworthy): here the runtime's
    own identity merge was skipped, so the event's usage, if any, is a bare
    partial reading no receipt should be built from.
    """
    live.unreceipted_calls += 1
    usage_key = f"call:{live.unreceipted_calls}"
    log.warning(
        "cc_runtime: no per-call usage row for %s (%s): %s",
        thread_id, usage_key, "measurement_incomplete",
    )
    turn_id = getattr(live.corsia_turn, "turn_id", None)
    if turn_id is None:
        return
    await run_in_db(
        report_unrecordable_call, thread_id, turn_id=turn_id, usage_key=usage_key,
        reason="measurement_incomplete",
        detail=(
            "final_call_usage arrived without this call's measured identity; "
            "the runtime's message-id merge was skipped for it"
        ),
    )


async def record_terminal_llm_usage(
    thread_id: str, live: _Live, usage: object, *, quiet_if_unmeasured: bool = False,
) -> None:
    """Persist the Turn's accumulated volume when none of its calls was receipted.

    ``usage`` is the final call's occupancy sample. Each call's usage is
    recorded as it arrives (``record_call_llm_usage``); the terminal hands
    the ledger the accumulated Turn volume as the fallback
    (``plans.usage_calls.record_turn_fallback``): written whole when the
    ledger holds no receipt of the Turn's calls, not at all when it does. A
    call whose receipt was refused is on record as that call's defect.

    Claude Code reports a result-event total, but its Turn-versus-resumed-session
    scope is unverified. Codex's own provider total proved that such a total can
    be session-scoped; persisting it here could re-bill every earlier Turn. The
    canonical ledger marks subscription usage cost as unknown.
    """
    volume = _consume_turn_volume(live, usage)
    _assert_terminal_occupancy(thread_id, live, usage)
    model_id = usage_model_id("cc", live)
    measured = (
        volume.ledger_row()
        if volume is not None
        else {}
    )
    if volume is None:
        log.warning("cc_runtime: recording unknown terminal usage for %s", thread_id)
    try:
        if model_id is None:
            # Nothing the ledger can take, but the loss is the Turn's task's
            # defect; without a Turn there is no task to mark.
            log.warning("cc_runtime: no usage row for %s because the model is unknown", thread_id)
            turn_id = getattr(live.corsia_turn, "turn_id", None)
            if turn_id is not None:
                await run_in_db(
                    report_unrecordable_call, thread_id, turn_id=turn_id, usage_key="terminal",
                    reason="model_unknown", detail="the provider named no model for this Turn",
                )
            return
        await run_in_db(
            record_turn_fallback,
            thread_id,
            model_id,
            measured,
            source="external_harness:claude-code",
            turn_id=getattr(live.corsia_turn, "turn_id", None),
            usage_key="terminal",
            harness=live.harness,
            reasoning_effort=usage_effort(live),
            quiet_if_unmeasured=quiet_if_unmeasured,
        )
    except (PlanUsagePersistenceError, UsageReceiptConflictError):
        # The entry has already put every failure it could on the Turn's task
        # as a usage_recording_failed defect; what reaches here is the one it
        # could not record. It may not escape: the caller's terminal path
        # broadcasts turn_complete and records plan completion inside a
        # try/finally with no except, so re-raising would trade an incomplete
        # accounting handoff for a wedged Turn and a result the user never
        # receives.
        log.error(
            "cc_runtime: plan-bound usage handoff incomplete for %s; "
            "turn completion continues",
            thread_id,
        )
    except Exception:
        # Reached only for a Turn bound to no task: the entry raises an
        # unexpected failure there because nothing durable exists to mark it
        # on. The log is the only surface, and the terminal must complete.
        log.exception(
            "cc_runtime: failed to persist terminal usage for %s", thread_id
        )
