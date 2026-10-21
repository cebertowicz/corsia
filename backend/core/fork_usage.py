"""Common measured-usage observer; each fork kind owns its attribution store."""

from __future__ import annotations

from dataclasses import replace

from loguru import logger

from infra.db_async import run_in_db
from infra.llm_usage_events import emit_llm_usage_recorded
from infra.vendor_turn_binding import checkpoint_account_id
from infra.turn_usage_receipts import PLAN_FIELDS
from llm.pricing import recorded_cost
from plans import public
from subagents.usage_model import detached_usage_model_id


async def bind_usage(request, *, source, freeze_binding, record_call):
    """Bind one observer using the kind's attribution and persistence functions."""
    binding = dict.fromkeys(PLAN_FIELDS)

    async def defect(key, error):
        logger.error("{} {} usage incomplete for {}: {}", source, request.fork_id, key, error)
        try:
            await run_in_db(
                public.report_usage_defect, request.thread_id, binding, None,
                f"{source}:{request.fork_id}:{key}", reason=f"{source}_usage_failed", detail=str(error),
            )
        except Exception:
            logger.exception("{} {} accounting defect could not be persisted", source, request.fork_id)

    account_id = None
    try:
        account_id = (request.vendor_account_id if request.vendor_account_bound else
                      await run_in_db(checkpoint_account_id, request.thread_id, request.target.harness))
        binding, errors = await freeze_binding(request, account_id)
        for error in errors:
            await defect("binding", error)
    except Exception as error:
        await defect("binding", error)

    async def on_usage(call_key, reported, measured):
        model = detached_usage_model_id(request.target, reported)
        measured = {
            **measured,
            "cost_usd": recorded_cost(model, measured, harness=request.target.harness),
        }
        try:
            inserted = await record_call(request, account_id, call_key, model, measured)
            if inserted:
                emit_llm_usage_recorded(request.thread_id, model, measured, source=source)
            if not public.usage_is_measured(measured):
                await defect(call_key, "provider usage was not measured")
        except Exception as error:
            await defect(call_key, error)

    return replace(request, on_usage=on_usage, vendor_account_id=account_id, vendor_account_bound=True)
