"""Fresh human input requests the chosen limited account's existing control read."""
from __future__ import annotations

from connections.account_receipts import capture_receipt
from connections.account_policy import chosen_account_id
from connections.harness_usage import project_for_model, stored_usage
from connections.harness_usage_check import schedule_login_check
from connections.usage_verdicts import availability_from_projection
from connections.vendor_accounts import VENDORS
from infra.database import get_db
from infra.db_async import run_in_db
from infra.thread_input_types import HeldAdmission


def _limited_receipt(held: HeldAdmission):
    # Admission already derived this provider through the canonical model resolver.
    provider = held.connection
    if provider not in VENDORS:
        return None
    db = get_db()
    try:
        db.execute("BEGIN")
        account_id = chosen_account_id(held.thread_id, provider, db=db)
        if account_id is None:
            return None
        usage = stored_usage(provider=provider, account_id=account_id, db=db)
        projection = project_for_model(usage, model=held.model, provider=provider)
        availability = availability_from_projection(provider, projection)
        if availability.state not in {"limited_with_reset", "limited_without_reset"}:
            return None
        receipt = capture_receipt(provider, account_id=account_id, db=db)
        return (provider, receipt) if receipt is not None else None
    finally:
        db.close()


async def request_held_usage_recheck(held: HeldAdmission) -> None:
    """Resolve current account/limit/receipt atomically, then submit without awaiting I/O."""
    subject = await run_in_db(_limited_receipt, held)
    if subject is not None:
        schedule_login_check(*subject)
