"""Checkpoint's Plans attribution and checkpoint-owned usage receipts."""

from __future__ import annotations

from core import fork_usage
from infra import checkpoint_usage
from infra.db_async import run_in_db
from infra.time import utc_now_iso
from infra.turn_usage_receipts import PLAN_FIELDS
from plans import public


async def _freeze_binding(request, account_id):
    errors = []
    binding = None
    try:
        binding = await run_in_db(public.binding_at_call_start, request.thread_id)
        if binding == public.UNATTRIBUTED:
            errors.append("checkpoint task attribution could not be read")
    except Exception as error:
        errors.append(str(error))
    binding = {field: binding.get(field) if binding else None for field in PLAN_FIELDS}
    try:
        await run_in_db(
            checkpoint_usage.freeze_binding, request.fork_id, binding,
            vendor_account_id=account_id,
        )
    except Exception as error:
        errors.append(str(error))
    return binding, errors


async def _record_call(request, account_id, call_key, model, measured):
    return await run_in_db(
        checkpoint_usage.record_call, request.fork_id, call_key, model, measured,
        now_iso=utc_now_iso(),
    )


async def bind_usage(request):
    """Freeze checkpoint attribution and observe its calls without blocking output."""
    return await fork_usage.bind_usage(
        request, source="checkpoint", freeze_binding=_freeze_binding, record_call=_record_call,
    )
