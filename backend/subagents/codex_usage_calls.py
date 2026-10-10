"""One spent Codex reading, one ledger receipt, as the provider reports them."""

from __future__ import annotations

import logging

from infra.db_async import run_in_db
from infra.turn_usage_receipts import UsageReceiptError
from plans.public import record_observed_call_usage, report_unrecordable_call
from subagents.usage_model import usage_effort, usage_model_id

log = logging.getLogger(__name__)


async def record_call_llm_usage(
    thread_id: str, live, *, provider_turn_id: object, ordinal: int, measured: object,
) -> None:
    """Persist one reading's newly spent volume as its own receipt.

    Stdout names no response id, so the receipt is keyed by the provider turn the
    reading belongs to and the reading's ordinal within it. A restarted
    app-server session continues the CORSIA Turn in a new provider turn and
    counts its readings from one again; keyed by both, those readings never
    meet the receipts the Turn already holds. A repeated reading spends
    nothing and never reaches here: early rollout receipts and later stdout
    totals pass through the same session observation before this writer.
    Durable as it arrives, before the turn
    ends. A receipt the entry could not write is a recorded defect there,
    and so is a reading the ledger cannot take at all (no model, turn or
    measurement); a failure the entry cannot even record escapes, as any
    failed write does.
    ``measured`` is the ledger row the reading spent; the canonical ledger
    marks subscription usage cost as unknown, as for the delta.
    """
    model_id = usage_model_id("codex", live)
    row = measured if isinstance(measured, dict) and measured else None
    turn = provider_turn_id if isinstance(provider_turn_id, str) and provider_turn_id else None
    if model_id is None or turn is None or row is None:
        reason = (
            "model_unknown" if model_id is None
            else "provider_turn_unnamed" if turn is None else "measurement_missing"
        )
        log.warning(
            "codex_runtime: no per-call usage row for %s (reading %s): %s",
            thread_id, ordinal, reason,
        )
        # Nothing the ledger can take, but the loss is the Turn's task's
        # defect; without a Turn there is no task to mark.
        turn_id = getattr(live.corsia_turn, "turn_id", None)
        if turn_id is not None:
            await run_in_db(
                report_unrecordable_call, thread_id, turn_id=turn_id,
                usage_key=f"reading:{turn or '?'}:{ordinal}", reason=reason,
                detail="the provider named no model, turn or measurement for this reading",
            )
        return
    await run_in_db(
        record_observed_call_usage,
        thread_id,
        model_id,
        dict(row),
        source="external_harness:codex",
        turn_id=getattr(live.corsia_turn, "turn_id", None),
        usage_key=f"reading:{turn}:{ordinal}",
        harness=live.harness,
        reasoning_effort=usage_effort(live),
    )


async def on_call_usage(ctx, ev: dict) -> None:
    """One spent reading of the active turn: its own ledger receipt.

    A failure the entry could not even record is logged here: the event
    dispatch carrying the Turn's result must not end on an accounting error.
    """
    if getattr(ctx.live, "corsia_turn", None) is None:
        return
    try:
        await record_call_llm_usage(
            ctx.thread_id, ctx.live, provider_turn_id=ev.get("turn_id"),
            ordinal=int(ev.get("ordinal") or 0), measured=ev.get("usage"),
        )
    except UsageReceiptError:
        log.exception(
            "codex_runtime: usage accounting failed for %s; event stream continues",
            ctx.thread_id,
        )
