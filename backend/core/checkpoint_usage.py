"""Connect one checkpoint's provider observations to its frozen Plans binding."""

from __future__ import annotations

from dataclasses import replace

from loguru import logger

from infra import checkpoint_usage
from infra.db_async import run_in_db
from infra.llm_usage_events import emit_llm_usage_recorded
from infra.time import utc_now_iso
from infra.turn_usage_receipts import PLAN_FIELDS
from infra.vendor_turn_binding import checkpoint_account_id
from plans import public
from llm.pricing import recorded_cost
from subagents.usage_model import detached_usage_model_id


async def bind_usage(request, *, checkpoint_owned: bool = True, source: str = "checkpoint"):
    """Freeze attribution before dispatch; accounting failure cannot stop output.

    A fork with no checkpoint row (``checkpoint_owned=False``, a document
    review) serves its requester, not the thread's current task, so it records
    each call unattributed straight to the ledger.
    """
    binding_error = None
    binding = None
    try:
        if checkpoint_owned:
            binding = await run_in_db(public.binding_at_call_start, request.thread_id)
            if binding == public.UNATTRIBUTED:
                binding_error = "checkpoint task attribution could not be read"
    except Exception as error:
        binding, binding_error = None, str(error)
    binding = {field: binding.get(field) if binding else None for field in PLAN_FIELDS}

    async def defect(key, error):
        logger.error("{} {} usage incomplete for {}: {}", source, request.fork_id, key, error)
        try:
            await run_in_db(
                public.report_usage_defect, request.thread_id, binding, None,
                f"{source}:{request.fork_id}:{key}", reason=f"{source}_usage_failed", detail=str(error),
            )
        except Exception:
            logger.exception("{} {} accounting defect could not be persisted", source, request.fork_id)

    if binding_error is not None:
        await defect("binding", binding_error)
    account_id = None
    try:
        account_id = (request.vendor_account_id if request.vendor_account_bound else
                      await run_in_db(checkpoint_account_id, request.thread_id, request.target.harness))
        if checkpoint_owned:
            await run_in_db(checkpoint_usage.freeze_binding, request.checkpoint_id, binding,
                            vendor_account_id=account_id)
    except Exception as error:
        await defect("binding", error)

    async def on_usage(call_key, reported, measured):
        model = detached_usage_model_id(request.target, reported)
        measured = {
            **measured,
            "cost_usd": recorded_cost(
                model,
                measured,
                harness=request.target.harness,
            ),
        }
        try:
            if checkpoint_owned:
                inserted = await run_in_db(
                    checkpoint_usage.record_call, request.checkpoint_id, call_key, model,
                    measured, now_iso=utc_now_iso(),
                )
            else:
                inserted = await run_in_db(
                    checkpoint_usage.record_fork_call, request.fork_id, request.thread_id,
                    call_key, model, measured, vendor_account_id=account_id, now_iso=utc_now_iso(), source=source,
                )
            if inserted:
                emit_llm_usage_recorded(request.thread_id, model, measured, source=source)
            if not public.usage_is_measured(measured):
                await defect(call_key, "provider usage was not measured")
        except Exception as error:
            await defect(call_key, error)

    return replace(request, on_usage=on_usage, vendor_account_id=account_id, vendor_account_bound=True)
