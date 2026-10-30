"""A lost sign-in on the account a Turn ran on signs out the login that Turn captured."""
from __future__ import annotations


def sign_out_turn_login(turn_id: str | None, provider: str, *, db=None, include_account: bool = False):
    """Whether the Turn ran on a ``provider`` account; if so, sign out the login it captured.

    The Turn's login receipt is the fence: a sign-in made after the Turn started is a
    newer login this error says nothing about, so it is left signed in. Every account
    stamps a receipt when its Turn is claimed, so no receipt means no account ran it.
    """
    if not turn_id:
        return False
    from connections.account_lifecycle import sign_out_expired
    from connections.vendor_accounts import get_account
    from infra.database import get_db
    from infra.vendor_login import read_turn_receipt

    connection = db or get_db()
    try:
        receipt = read_turn_receipt(turn_id, db=connection)
        if receipt is None:
            return False
        account = get_account(receipt.account_id, db=connection)
        if account["provider"] != provider or account["source_home"] is None:
            return False
        updated = sign_out_expired(receipt, db=connection)
        return updated if include_account else True
    finally:
        if db is None:
            connection.close()


async def claude_turn_signed_out(turn_id: str | None, detail: str | None, *, db=None, include_account: bool = False):
    """Sign out the Turn's added Claude login on the exact expired-login error, before the Turn completes."""
    from connections.auth_failure import auth_failure_kind
    from infra.db_async import run_in_db_unless_owned

    if not auth_failure_kind("claude-code", detail):
        return False
    return await run_in_db_unless_owned(
        db, sign_out_turn_login, turn_id, "claude-code", db=db, include_account=include_account,
    )
