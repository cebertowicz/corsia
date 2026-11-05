"""The one Messages row of this computer: find the one a thread may use, add it, or turn it back on."""

from __future__ import annotations

import sqlite3

from connections.account_state import emit_lifecycle_event, mark_signed_in, record_audit
from connections.connected_accounts import (
    MESSAGES_PROVIDER, ConnectedAccountError, create_account_for_thread, get_account, visible_accounts,
)
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db

ADDRESS = "This computer"


def _own_db(job, thread_id: str):
    db = get_db()
    try:
        return job(thread_id, db)
    finally:
        db.close()


def _usable(thread_id: str, db: sqlite3.Connection) -> dict | None:
    return next(
        (row for row in visible_accounts(thread_id, db=db, provider=MESSAGES_PROVIDER) if row["state"] == "connected"),
        None,
    )


async def usable_for_thread(thread_id: object) -> dict | None:
    """The connected Messages row whose reach includes this thread, or None."""
    return await run_in_db(_own_db, _usable, thread_id) if isinstance(thread_id, str) and thread_id else None


def _connect(thread_id: str, db: sqlite3.Connection) -> tuple[dict | None, bool]:
    """``(row, created)``: the row this thread reaches, or a new one that waits for the person's Sign in.

    The look and the insert share one transaction, so two connects cannot both add a row. A new row is
    created off (``needs_sign_in``) at the adding thread's reach; only the person turns it on.
    """
    with write_transaction(db, scope="messages_connect") as conn:
        reached = visible_accounts(thread_id, db=conn, provider=MESSAGES_PROVIDER)
        if reached:
            return next((row for row in reached if row["state"] == "connected"), reached[0]), False
        if visible_accounts(None, db=conn, provider=MESSAGES_PROVIDER):
            return None, False
        row = create_account_for_thread(
            MESSAGES_PROVIDER, ADDRESS, actor_thread_id=thread_id, created_by=("agent", thread_id),
            state="needs_sign_in", db=conn,
        )
        record_audit(str(row["id"]), "connect", ("agent", thread_id), db=conn)
    emit_lifecycle_event("connect_started", str(row["id"]), MESSAGES_PROVIDER)
    return row, True


def _turn_on(account_id: str, actor: tuple[str, str], db: sqlite3.Connection) -> bool:
    """Turn an existing Messages row on at its own reach; False when it is not a Messages row."""
    with write_transaction(db, scope="messages_turn_on") as conn:
        try:
            row = get_account(account_id, db=conn)
        except ConnectedAccountError:
            return False
        if row["provider"] != MESSAGES_PROVIDER:
            return False
        if row["state"] == "connected":
            return True
        mark_signed_in(account_id, address=str(row["address"]), provider_subject=None, actor=actor, db=conn)
        record_audit(account_id, "sign_in", actor, db=conn)
    if row["state"] == "signed_out":
        emit_lifecycle_event("connect_started", account_id, MESSAGES_PROVIDER)
    emit_lifecycle_event("connect_succeeded", account_id, MESSAGES_PROVIDER)
    return True


def _state_of(account_id: str, db: sqlite3.Connection) -> str | None:
    try:
        row = get_account(account_id, db=db)
    except ConnectedAccountError:
        return None
    return str(row["state"]) if row["provider"] == MESSAGES_PROVIDER else None


async def state_of(account_id: str) -> str | None:
    """A Messages row's state, or None when the id is not a Messages row."""
    return await run_in_db(_own_db, _state_of, account_id)


async def connect(thread_id: str) -> tuple[dict | None, bool]:
    return await run_in_db(_own_db, _connect, thread_id)


async def turn_on(account_id: str, actor: tuple[str, str]) -> bool:
    """Settings › Connected apps: Enable on an off Messages row."""
    return await run_in_db(_own_db, lambda _id, db: _turn_on(_id, actor, db), account_id)
