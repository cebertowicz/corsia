"""Commit metadata lifecycle and durable holds before vendor control cleanup."""
from __future__ import annotations

from connections.account_holds import refresh_account_holds
from connections.vendor_accounts import get_account, set_account_state
from infra.database import write_transaction


def change_state(account_id: str, state: str, *, expected_revision: int, db=None) -> dict:
    """Retain identity, binding, model and frozen spend on signout/removal."""
    with write_transaction(db, scope="vendor_account_hold_lifecycle") as connection:
        account = set_account_state(account_id, state, expected_revision=expected_revision, db=connection)
        refresh_account_holds(account_id, db=connection)
        return account


def sign_out_expired(account_id: str, *, db=None) -> dict:
    """Sign out an added account whose own Turn ended on the vendor's exact expired-login error.

    That error is the definite answer, as it is for the Default source, so a stale
    successful status read cannot leave the account selectable with no way to sign in.
    """
    with write_transaction(db, scope="vendor_account_hold_lifecycle") as connection:
        account = get_account(account_id, db=connection)
        if account["source_home"] is None or account["state"] != "signed_in":
            return account
        account = set_account_state(account_id, "signed_out", expected_revision=account["revision"], db=connection)
        refresh_account_holds(account_id, db=connection)
        return account
