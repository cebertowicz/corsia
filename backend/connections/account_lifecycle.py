"""Commit metadata lifecycle and durable holds before vendor control cleanup."""
from __future__ import annotations

from connections.account_holds import refresh_account_holds
from connections.vendor_accounts import set_account_state
from infra.database import write_transaction


def change_state(account_id: str, state: str, *, expected_revision: int, db=None) -> dict:
    """Retain identity, binding, model and frozen spend on signout/removal."""
    with write_transaction(db, scope="vendor_account_hold_lifecycle") as connection:
        account = set_account_state(account_id, state, expected_revision=expected_revision, db=connection)
        refresh_account_holds(account_id, db=connection)
        return account
