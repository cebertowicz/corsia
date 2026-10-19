"""The one gate that keeps a checkpoint fold off an account-limited target (#2477)."""

from __future__ import annotations

from core.checkpoint_types import (
    CheckpointCloudOff, CheckpointTargetLimited, ProviderCheckpointRequest,
)
from infra.db_async import run_in_db


async def refuse_limited_target(provider_request: ProviderCheckpointRequest) -> None:
    """Read the verdict for the request's own target, never the thread's bound model.

    A DB read only: it creates no row, arms nothing, and sits before any wait,
    so Stop never passes through it. A bound request is checked on the account
    it will launch on; an unbound one on the thread's chosen account, which is
    the account ``admit_checkpoint`` claims for it.
    """
    from infra.held_admission import limited_admission

    account_id = (
        provider_request.vendor_account_id if provider_request.vendor_account_bound else None
    )
    held = await run_in_db(
        limited_admission, provider_request.thread_id,
        provider_request.target.catalog_id, account_id=account_id,
    )
    if held is not None:
        raise CheckpointTargetLimited(held)


async def refuse_cloud_off_target(provider_request: ProviderCheckpointRequest) -> None:
    """Refuse a fold whose own target the thread's project may not use, before any provider call.

    Eligibility feeds previews and idle markers that were read earlier, so the policy is read
    again here, at the one dispatch point. A DB read only: it creates no row and arms nothing.
    """
    from infra.database import get_db
    from llm.locality import thread_locality_refusal

    def refused() -> str | None:
        db = get_db()
        try:
            return thread_locality_refusal(
                provider_request.target.catalog_id, thread_id=provider_request.thread_id, db=db,
            )
        finally:
            db.close()

    if await run_in_db(refused):
        raise CheckpointCloudOff()
