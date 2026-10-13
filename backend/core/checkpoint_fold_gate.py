"""The one gate that keeps a checkpoint fold off an account-limited target (#2477)."""

from __future__ import annotations

from core.checkpoint_types import CheckpointTargetLimited, ProviderCheckpointRequest
from infra.db_async import run_in_db


def _target_turn_account(thread_id: str, harness: str, model: str):
    """Read the canonical account a new target-model turn would claim."""
    from connections.account_policy import turn_account
    from infra.database import get_db

    db = get_db()
    try:
        return turn_account(harness, thread_id=thread_id, model=model, db=db)
    finally:
        db.close()


async def refuse_limited_target(provider_request: ProviderCheckpointRequest) -> None:
    """Read the verdict for the request's own target, never the thread's bound model.

    A DB read only: it creates no row, arms nothing, and sits before any wait,
    so Stop never passes through it.
    """
    from infra.held_admission import limited_admission

    if provider_request.vendor_account_bound:
        account_id = provider_request.vendor_account_id
    else:
        account_id, _reason = await run_in_db(
            _target_turn_account, provider_request.thread_id,
            provider_request.target.harness, provider_request.target.catalog_id,
        )
    held = await run_in_db(
        limited_admission, provider_request.thread_id,
        provider_request.target.catalog_id, account_id=account_id,
    )
    if held is not None:
        raise CheckpointTargetLimited(held)
