"""Account captured by a Turn, so a lost sign-in is checked on that account."""
from __future__ import annotations


def turn_account(turn_id: str | None, provider: str, *, db=None) -> dict | None:
    if not turn_id:
        return None
    from connections.vendor_accounts import get_account
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
        return {"id": account["id"], "custom": account["source_home"] is not None}
    finally:
        if db is None:
            connection.close()


async def claude_exit_account(turn_id: str, detail: str, *, db=None) -> dict | None:
    from connections.auth_failure import auth_failure_kind
    from infra.db_async import run_in_db_unless_owned

    if not auth_failure_kind("claude-code", detail):
        return None
    return await run_in_db_unless_owned(db, turn_account, turn_id, "claude-code", db=db)
