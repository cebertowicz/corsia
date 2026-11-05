"""A lost sign-in on the account a Turn ran on signs out the login that Turn captured."""
from __future__ import annotations


def _turn_login(turn_id: str | None, provider: str, *, db=None):
    """The login receipt of the added ``provider`` account the Turn ran on, else None.

    Every account stamps a receipt when its Turn is claimed, so no receipt means no account ran it.
    """
    if not turn_id:
        return None
    from connections.vendor_accounts import get_account
    from infra.database import get_db
    from infra.vendor_login import read_turn_receipt

    connection = db or get_db()
    try:
        receipt = read_turn_receipt(turn_id, db=connection)
        if receipt is None:
            return None
        account = get_account(receipt.account_id, db=connection)
        if account["provider"] != provider or account["source_home"] is None:
            return None
        return receipt
    finally:
        if db is None:
            connection.close()


def sign_out_turn_login(receipt, *, db=None, include_account: bool = False):
    """Sign out the login ``receipt`` captured.

    The receipt is the fence: a sign-in made after the Turn started is a newer
    login this error says nothing about, so it is left signed in.
    """
    from connections.account_lifecycle import sign_out_expired

    updated = sign_out_expired(receipt, db=db)
    return updated if include_account else True


async def claude_login_verdict(turn_id: str | None, detail: str | None, *, db=None):
    """On the exact expired-login error, check the Turn's added Claude login once.

    The CLI can report an expired login while the account's login still answers.
    Returns None when no added account ran the Turn on that error, else
    ``(receipt, answered)``. The check can take its full bound, so callers run it
    outside any lock another action waits on.
    """
    from connections.auth_failure import auth_failure_kind
    from connections.harness_usage_check import check_login
    from infra.db_async import run_in_db_unless_owned

    if not auth_failure_kind("claude-code", detail):
        return None
    receipt = await run_in_db_unless_owned(db, _turn_login, turn_id, "claude-code", db=db)
    if receipt is None:
        return None
    return receipt, await check_login("claude-code", receipt) == "pass"


def login_race_answered(detail: str | BaseException | None, verdict) -> bool:
    """Only the CLI OAuth refresh error with an answering login can be re-run."""
    from connections.auth_failure import claude_oauth_refresh_failure

    return login_answered(verdict) and claude_oauth_refresh_failure(detail)


def login_answered(verdict) -> bool:
    """Whether a ``claude_login_verdict`` found the Turn's login still answering."""
    return verdict is not None and verdict[1]


async def sign_out_on_verdict(verdict, *, db=None, include_account: bool = False):
    """Sign out the receipt's login unless the verdict found it answering.

    False when nothing was signed out (no added account ran the Turn, or its
    login answered); otherwise ``sign_out_turn_login``'s result.
    """
    from infra.db_async import run_in_db_unless_owned

    if verdict is None or login_answered(verdict):
        return False
    return await run_in_db_unless_owned(
        db, sign_out_turn_login, verdict[0], db=db, include_account=include_account,
    )
