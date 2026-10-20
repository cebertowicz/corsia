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
from connections.claude_unlisted import base_identity
from llm.pricing import recorded_cost


def _recorded_model(request, reported: str) -> str:
    """Keep a vendor-reported Claude identity when the checkpoint supplied one."""
    if request.target.harness == "claude-code" and reported:
        return f"claude-code/{base_identity(reported)}"
    return request.target.catalog_id


async def bind_usage(request):
    """Freeze attribution before dispatch; accounting failure cannot stop output."""
    binding_error = None
    try:
        binding = await run_in_db(public.binding_at_call_start, request.thread_id)
        if binding == public.UNATTRIBUTED:
            binding_error = "checkpoint task attribution could not be read"
    except Exception as error:
        binding, binding_error = None, str(error)
    binding = {field: binding.get(field) if binding else None for field in PLAN_FIELDS}

    async def defect(key, error):
        logger.error("checkpoint {} usage incomplete for {}: {}", request.checkpoint_id, key, error)
        try:
            await run_in_db(
                public.report_usage_defect, request.thread_id, binding, None,
                f"checkpoint:{request.checkpoint_id}:{key}", reason="checkpoint_usage_failed", detail=str(error),
            )
        except Exception:
            logger.exception("checkpoint {} accounting defect could not be persisted", request.checkpoint_id)

    if binding_error is not None:
        await defect("binding", binding_error)
    account_id = None
    try:
        account_id = (request.vendor_account_id if request.vendor_account_bound else
                      await run_in_db(checkpoint_account_id, request.thread_id, request.target.harness))
        await run_in_db(checkpoint_usage.freeze_binding, request.checkpoint_id, binding,
                        vendor_account_id=account_id)
    except Exception as error:
        await defect("binding", error)

    async def on_usage(call_key, model, measured):
        model = _recorded_model(request, model)
        measured = {
            **measured,
            "cost_usd": recorded_cost(
                model,
                measured,
                harness=request.target.harness,
            ),
        }
        try:
            inserted = await run_in_db(
                checkpoint_usage.record_call, request.checkpoint_id, call_key, model,
                measured, now_iso=utc_now_iso(),
            )
            if inserted:
                emit_llm_usage_recorded(request.thread_id, model, measured, source="checkpoint")
            if not public.usage_is_measured(measured):
                await defect(call_key, "provider usage was not measured")
        except Exception as error:
            await defect(call_key, error)

    return replace(request, on_usage=on_usage, vendor_account_id=account_id, vendor_account_bound=True)
