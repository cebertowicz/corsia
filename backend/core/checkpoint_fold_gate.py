"""The one gate that keeps a checkpoint fold off an account-limited target (#2477)."""

from __future__ import annotations

from core.checkpoint_types import CheckpointTargetLimited, ProviderCheckpointRequest
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
