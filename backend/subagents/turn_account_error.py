"""Account identity captured by a Turn, for its provider error messages."""
from __future__ import annotations


def turn_account(turn_id: str | None, provider: str, *, db=None) -> dict | None:
    if not turn_id:
        return None
    from connections.account_profiles import account_label
    from connections.vendor_accounts import get_account, list_accounts
    from infra.database import get_db

    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT vendor_account_id FROM active_turns WHERE id=?", (turn_id,),
        ).fetchone()
        if row is None or row["vendor_account_id"] is None:
            return None
        account = get_account(row["vendor_account_id"], db=connection)
        if account["provider"] != provider:
            return None
        rows = list_accounts(provider=provider, db=connection)
        return {"id": account["id"], "name": account_label(account, rows=rows)["text"],
                "custom": account["source_home"] is not None}
    finally:
        if db is None:
            connection.close()


async def claude_exit_account(turn_id: str, detail: str, *, db=None) -> dict | None:
    from connections.auth_failure import auth_failure_kind
    from infra.db_async import run_in_db_unless_owned

    if not auth_failure_kind("claude-code", detail):
        return None
    return await run_in_db_unless_owned(db, turn_account, turn_id, "claude-code", db=db)


def claude_login_message(account: dict | None) -> str:
    if account and account["custom"]:
        return f"{account['name']} is signed out of Claude Code. Sign it in again under Settings › Providers."
    return "Claude Code is signed out on this computer. Sign in again under Settings › Providers."
