"""Atomic sign-in completion: identity and state land in one transaction."""
from __future__ import annotations

from connections import harness_usage
from connections.account_lifecycle import change_state
from connections.account_receipts import account_receipt
from connections.vendor_accounts import (
    AccountError,
    comparison_facts,
    duplicate_of,
    get_account,
    list_accounts,
    record_auth_cleanup,
    record_evidence,
)
from infra.database import write_transaction


class SameAccount(Exception):
    """A completed login repeats an account that is already signed in."""


def commit_signed_in(account_id: str, revision: int, identity: dict[str, str]):
    """Identity and state land together, only on the revision the sign-in started from.

    A signed-in repeat is refused before any write. Changed comparison facts
    clear usage in this transaction. A match with a signed-out row may complete
    and names that row; the last cleanup result is cleared.
    """
    with write_transaction(None, scope="vendor_account_signin") as connection:
        account = get_account(account_id, db=connection)
        if account["revision"] != revision or account["state"] == "removed":
            raise AccountError("account changed")
        proposed = account | {"identity": identity}
        blockers = [row for row in list_accounts(provider=account["provider"], db=connection)
                    if row["state"] == "signed_in" or row["source_home"] is None]
        repeat = duplicate_of(proposed, rows=blockers)
        if repeat is not None:
            raise SameAccount(repeat)
        before, after = comparison_facts(account), comparison_facts(proposed)
        if before is not None and after is not None and before != after:
            harness_usage.clear_account_usage(account["provider"], account_id, db=connection)
        record_evidence(account_id, identity=identity, expected_revision=revision, replace_comparison=True, db=connection)
        record_auth_cleanup(account_id, None, db=connection)
        change_state(account_id, "signed_in", expected_revision=revision, db=connection)
        completed = get_account(account_id, db=connection)
        return duplicate_of(completed, db=connection), account_receipt(completed)
