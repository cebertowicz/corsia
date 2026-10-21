"""Review usage belongs to the requester, never the reviewer's current task."""

from core import fork_usage
from infra import checkpoint_usage
from infra.db_async import run_in_db
from infra.time import utc_now_iso
from infra.turn_usage_receipts import PLAN_FIELDS


async def _unattributed(request, account_id):
    return dict.fromkeys(PLAN_FIELDS), ()


async def _record_call(request, account_id, call_key, model, measured):
    return await run_in_db(
        checkpoint_usage.record_fork_call, request.fork_id, request.thread_id,
        call_key, model, measured, vendor_account_id=account_id,
        now_iso=utc_now_iso(), source="artifact_review",
    )


async def bind_usage(request):
    """Bind review usage directly to the ledger without a checkpoint receipt."""
    return await fork_usage.bind_usage(
        request, source="artifact_review", freeze_binding=_unattributed, record_call=_record_call,
    )
