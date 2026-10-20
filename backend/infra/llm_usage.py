"""Canonical persistence for provider-reported LLM usage.

Historical boundary (2026-09-17, issue #1750): rows with
``source = 'external_harness:claude-code'`` created before this fix's deploy
understate ``completion_tokens`` by roughly 20-25x. Claude Code's stream-json
"assistant" frames report a still-generating usage snapshot per content
block; the per-call receipt fired from that stale snapshot instead of the
call's true final count on ``message_delta``. The input side (prompt, cache
read/write) was always correct -- only output was affected. The true
per-call output counts were never durably captured, so no backfill is
possible; ``created_at`` against this fix's deploy is the only available
distinguishing key. See issue #1894 for the consumer-side disclosure of this
gap.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
import uuid

from loguru import logger

from infra import database
from infra.database import write_transaction
from infra.llm_usage_events import cache_read_fraction, emit_llm_usage_recorded
from infra.product_events import emit_product_event
from infra.retained_usage import HeldUsage, hold_usage
from infra.time import utc_now_iso
from llm.resolution import Harness
from infra.turn_usage_receipts import (
    PLAN_FIELDS,
    UsageReceiptConflictError,
    UsageReceiptError,
    project_turn_usage_receipt,
    reconcile_pending_turn_usage,
    store_turn_usage_receipt,
    write_usage_row,
)


class PlanUsagePersistenceError(UsageReceiptError):
    """A plan-bound provider call's accounting defect could not be recorded.

    Deliberately non-propagating past its call sites. The ledger itself never
    raises it: a call it cannot write is held until its Turn ends
    (``infra.retained_usage``). ``plans.usage_calls`` raises it when the
    visible defect for a call cannot be written either. It must never reach
    the terminal path: preserving an already-paid provider result outranks
    the independently failing report.
    """


_PLAN_FIELDS = PLAN_FIELDS


def _turn_binding(db, thread_id: str, turn_id: str | None) -> dict[str, Any]:
    if turn_id is None:
        return {field: None for field in _PLAN_FIELDS}
    row = db.execute(
        """SELECT plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,
                  plan_usage_role
           FROM active_turns
           WHERE id=? AND thread_id=?""",
        (turn_id, thread_id),
    ).fetchone()
    if row is None:
        logger.warning(
            "llm_usage: no active Turn {} for {}; recording usage unbound",
            turn_id,
            thread_id,
        )
        return {field: None for field in _PLAN_FIELDS}
    return {field: row[field] for field in _PLAN_FIELDS}


def _prepare_turn_usage(
    db: Any,
    thread_id: str,
    turn_id: str | None,
    usage_key: str | None,
    model: str,
    measured: Mapping[str, Any],
    ttft_ms: int | None,
    source: str,
    now_iso: str,
    binding: Mapping[str, Any] | None = None,
    reasoning_effort: str | None = None,
) -> tuple[str, bool, Mapping[str, Any]] | None:
    if turn_id is None:
        return None
    if usage_key is None:
        raise UsageReceiptConflictError(
            "Turn-bound usage requires a stable usage_key"
        )
    return store_turn_usage_receipt(
        db,
        thread_id=thread_id,
        turn_id=turn_id,
        usage_key=usage_key,
        model=model,
        measured=measured,
        ttft_ms=ttft_ms,
        source=source,
        now_iso=now_iso,
        binding=binding,
        reasoning_effort=reasoning_effort,
    )


def _write_direct_usage(
    db: Any,
    usage_id: str,
    thread_id: str,
    turn_id: str | None,
    model: str,
    measured: Mapping[str, Any],
    ttft_ms: int | None,
    binding: Mapping[str, Any],
    source: str,
    now_iso: str,
    vendor_account_id: str | None = None,
    reasoning_effort: str | None = None,
) -> None:
    with write_transaction(db, scope="direct_llm_usage") as connection:
        write_usage_row(
            connection,
            usage_id=usage_id,
            thread_id=thread_id,
            turn_id=turn_id,
            model=model,
            measured=measured,
            ttft_ms=ttft_ms,
            binding=binding,
            source=source,
            now_iso=now_iso,
            vendor_account_id=vendor_account_id,
            reasoning_effort=reasoning_effort,
        )


def _frozen_account(db, thread_id, turn_id, direct_account):
    if turn_id is None:
        return direct_account  # Actual harness launch supplied this; unknown/native callers omit it.
    row = db.execute("SELECT vendor_account_id FROM active_turns WHERE id=? AND thread_id=?",
                     (turn_id, thread_id)).fetchone()
    return row["vendor_account_id"] if row else None


def _measured_usage(model: str, usage: Mapping[str, Any] | None, harness: Harness | None) -> dict[str, Any]:
    """Attach the call-time cost to one provider's reported token buckets."""
    from llm.pricing import recorded_cost

    return {**(usage or {}), "cost_usd": recorded_cost(model, usage or {}, harness=harness)}


def record_llm_usage(
    thread_id: str,
    model: str,
    usage: Mapping[str, Any] | None,
    source: str = "conversation",
    ttft_ms: int | None = None,
    *,
    turn_id: str | None = None,
    usage_key: str | None = None,
    binding: Mapping[str, Any] | None = None,
    vendor_account_id: str | None = None,
    harness: Harness | None = None,
    reasoning_effort: str | None = None,
    usage_id: str | None = None,
    _emit_product_event: Callable[..., None] | None = None,
) -> None:
    """Durably hand one provider call to the canonical usage ledger.

    Turn-owned calls require a stable producer key. The pending measurement is
    committed on the retained Turn before ledger projection; direct unbound
    callers keep their one-write path. A call whose write cannot commit is
    held and written by its thread's Turn terminal (``infra.retained_usage``);
    only a measurement conflict on a stable key raises.

    ``binding`` is the plan attribution the call belongs to as it is recorded,
    resolved by the caller (Plans owns that answer); a Turn-bound call with
    none falls back to the Turn's own snapshot.

    Each write claims its own standalone transaction. The attribution read
    does not own the pooled connection; only the canonical write owner may
    settle or release it. Receipt and projection remain separate commits.
    """
    measured = _measured_usage(model, usage, harness)
    emit = _emit_product_event or emit_product_event
    usage_id = usage_id or str(uuid.uuid4())[:12]
    now_iso = utc_now_iso()
    persisted = False
    frozen_account = vendor_account_id if turn_id is None else None
    try:
        db = database.get_db()
        frozen_account = _frozen_account(db, thread_id, turn_id, vendor_account_id)
        attribution: Mapping[str, Any] = _turn_binding(db, thread_id, turn_id)
        if turn_id is not None and binding is not None:
            attribution = {field: binding.get(field) for field in _PLAN_FIELDS}
        turn_handoff_complete = False
        stored = _prepare_turn_usage(
            None, thread_id, turn_id, usage_key, model, measured, ttft_ms, source,
            now_iso, binding=attribution if turn_id is not None else None, reasoning_effort=reasoning_effort,
        )
        if turn_id is not None:
            if stored is not None:
                turn_handoff_complete = True
                identity, already_projected, attribution = stored
                if not already_projected:
                    projection = project_turn_usage_receipt(None, turn_id, identity)
                    persisted = bool(projection and projection.inserted)
        if not turn_handoff_complete:
            if turn_id is not None:
                attribution = {field: None for field in _PLAN_FIELDS}
            _write_direct_usage(
                None, usage_id, thread_id, turn_id, model, measured, ttft_ms, attribution,
                source, now_iso, frozen_account, reasoning_effort,
            )
            persisted = True
    except UsageReceiptConflictError as exc:
        logger.warning("llm_usage: failed to persist usage for {}: {}", thread_id, exc)
        raise
    except Exception as exc:
        logger.warning("llm_usage: usage for {} held until its Turn ends: {}", thread_id, exc)
        hold_usage(HeldUsage(
            thread_id=thread_id, turn_id=turn_id, usage_key=usage_key, usage_id=usage_id,
            model=model, measured=measured, ttft_ms=ttft_ms,
            binding=binding if turn_id is not None else None, source=source, now_iso=now_iso,
            vendor_account_id=frozen_account, reasoning_effort=reasoning_effort,
        ))
    if persisted:
        emit_llm_usage_recorded(thread_id, model, measured, source=source,
                                ttft_ms=ttft_ms, _emit_product_event=emit)
