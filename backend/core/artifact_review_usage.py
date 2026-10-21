"""review fork spend is recorded on the reviewer thread without a plan binding; per-request attribution is a later Structured Plans read"""

from core import fork_usage
from infra.fork_usage import record_fork_call
from infra.db_async import run_in_db
from infra.time import utc_now_iso
from infra.turn_usage_receipts import PLAN_FIELDS


async def _unattributed(request, account_id):
    return dict.fromkeys(PLAN_FIELDS), ()


async def _record_call(request, account_id, call_key, model, measured):
    return await run_in_db(
        record_fork_call, request.fork_id, request.thread_id,
        call_key, model, measured, vendor_account_id=account_id,
        now_iso=utc_now_iso(), source="artifact_review",
    )


async def bind_usage(request):
    """Bind review usage directly to the ledger without a checkpoint receipt."""
    return await fork_usage.bind_usage(
        request, source="artifact_review", freeze_binding=_unattributed, record_call=_record_call,
    )
