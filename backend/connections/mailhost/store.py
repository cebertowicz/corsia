"""A mail-host row's settings and check result, kept in ``endpoints_json``.

The JSON holds the non-secret settings plus a ``check`` object: ``status``, the ``host`` a
failure names, and the ``token`` of the check that wrote it. A finishing check writes only if
its token is still the row's, so a newer Set or Clear is never overwritten by an older check.
The shared row state stays ``connected`` or ``needs_sign_in``: a row is connected only after
its check passed, and ``capabilities_json`` lists exactly what that check proved.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from connections.mailhost.endpoints import Endpoints
from infra import key_store
from infra.database import get_db, write_transaction
from infra.time import utc_now_iso

WAITING, CHECKING, OK, MAIL_ONLY = "waiting", "checking", "ok", "mail_only"
REFUSED, REVOKED, IMAP_OFF, UNREACHABLE = "refused", "revoked", "imap_off", "unreachable"
CONNECTED_STATUSES = frozenset((OK, MAIL_ONLY))
CANNOT_STORE = "This password has a space or a character Keys can’t store. If {host} offers app passwords, make one and paste that."
_WHITESPACE = re.compile(r"\s+")


def _read(row: sqlite3.Row | None) -> tuple[Endpoints | None, dict[str, Any]]:
    try:
        raw = json.loads(row["endpoints_json"]) if row is not None and row["endpoints_json"] else None
    except (TypeError, ValueError):
        raw = None
    check = raw.get("check") if isinstance(raw, dict) else None
    return Endpoints.from_dict(raw), check if isinstance(check, dict) else {}


def read(account_id: str, *, db: sqlite3.Connection) -> tuple[Endpoints | None, dict[str, Any]]:
    """The row's settings and its check object; ``(None, {})`` for anything that isn't a mail-host row."""
    return _read(db.execute(
        "SELECT endpoints_json FROM connected_accounts WHERE id=? AND provider='mailhost'", (account_id,),
    ).fetchone())


def account_id_for_key(name: str) -> str | None:
    """The account id a mail-host key entry name carries, or None for any other name."""
    if not name.startswith(key_store.MAILHOST_PREFIX):
        return None
    suffix = name[len(key_store.MAILHOST_PREFIX):]
    return suffix if key_store.ACCOUNT_ID_PATTERN.match(suffix) else None


def normalize_pasted(name: str, value: object, db: sqlite3.Connection) -> object:
    """A pasted app password as it will be stored (H6).

    A known host's app passwords never hold whitespace, so all of it goes. A typed-in server's
    value is kept as given, and one the store can't keep gets its own line.
    """
    account_id = account_id_for_key(name)
    endpoints = read(account_id, db=db)[0] if account_id and isinstance(value, str) else None
    if endpoints is None or not isinstance(value, str):
        return value
    if endpoints.known:
        return _WHITESPACE.sub("", value)
    try:
        key_store.validated_value(value)
    except key_store.KeyStoreError:
        if value.strip():
            raise key_store.KeyStoreError(CANNOT_STORE.format(host=endpoints.name)) from None
    return value


def write(
    account_id: str, endpoints: Endpoints, *, status: str, host: str = "", token: str | None = None,
    capabilities: tuple[str, ...] = (), expect_token: str | None = None, db: sqlite3.Connection | None = None,
) -> bool:
    """Save settings, check result, state and capabilities in one write.

    With ``expect_token`` the write happens only while the row's check token still equals it.
    Returns whether it was written.
    """
    check = {"status": status, "host": host, "token": token, "at": utc_now_iso()}
    state = "connected" if status in CONNECTED_STATUSES else "needs_sign_in"
    with write_transaction(db, scope="mail_host_check") as conn:
        if expect_token is not None and read(account_id, db=conn)[1].get("token") != expect_token:
            return False
        return conn.execute(
            "UPDATE connected_accounts SET endpoints_json=?,state=?,state_cause=NULL,capabilities_json=?,updated_at=? "
            "WHERE id=? AND provider='mailhost'",
            (json.dumps({**endpoints.as_dict(), "check": check}, separators=(",", ":")), state,
             json.dumps(list(capabilities), separators=(",", ":")), utc_now_iso(), account_id),
        ).rowcount == 1


def demote_revoked(account_id: str, expected_check: dict[str, Any], *, db: sqlite3.Connection | None = None) -> bool:
    """Mark a password revoked only if the exact check snapshot is still current.

    A key replacement or server edit may finish while an IMAP read is in flight. The
    comparison and write share one transaction, and the write preserves the row's
    then-current endpoints instead of restoring the stale reader snapshot.
    """
    with write_transaction(db, scope="mail_host_demote") as conn:
        endpoints, current_check = read(account_id, db=conn)
        if endpoints is None or current_check != expected_check:
            return False
        return write(account_id, endpoints, status=REVOKED, db=conn)


def begin_check(
    account_id: str, *, replacement: Endpoints | None = None, only_if_idle: bool = False,
    db: sqlite3.Connection | None = None,
) -> tuple[Endpoints, str] | None:
    """Mark the row Checking; returns its settings and the new check's token, or None for no such row.

    With ``replacement`` the check runs against those settings, which are saved in the same write.
    With ``only_if_idle`` a row already Checking is left alone and the token is empty: the check
    running on it is the one, so no second starts.
    """
    with write_transaction(db, scope="mail_host_check_begin") as conn:
        endpoints, state = read(account_id, db=conn)
        if endpoints is None:
            return None
        if only_if_idle and state.get("status") == CHECKING:
            return endpoints, ""
        endpoints = replacement or endpoints
        token = uuid.uuid4().hex
        write(account_id, endpoints, status=CHECKING, token=token, db=conn)
        return endpoints, token


def settle_orphaned_checks(*, db: sqlite3.Connection | None = None) -> int:
    """End every row still Checking as Server unreachable, which the person can retry; returns how many.

    Only for a process that has just started: no check can be running then, so a row still
    Checking is one whose process died mid-check.
    """
    with write_transaction(db, scope="mail_host_settle") as conn:
        stuck = [str(r["id"]) for r in conn.execute(
            "SELECT id FROM connected_accounts WHERE provider='mailhost' AND endpoints_json IS NOT NULL"
        ) if read(str(r["id"]), db=conn)[1].get("status") == CHECKING]
        for account_id in stuck:
            endpoints = read(account_id, db=conn)[0]
            if endpoints is not None:
                write(account_id, endpoints, status=UNREACHABLE, host=endpoints.imap.host, db=conn)
        return len(stuck)


def reset_to_waiting(account_id: str, *, db: sqlite3.Connection | None = None) -> bool:
    """After Clear: the row waits for an app password again, with no capability and no check pending."""
    with write_transaction(db, scope="mail_host_waiting") as conn:
        endpoints = read(account_id, db=conn)[0]
        if endpoints is not None:
            return write(account_id, endpoints, status=WAITING, db=conn)
        return False


def row_exists_for_address(address: str, *, db: sqlite3.Connection | None = None) -> str | None:
    """The id of the mail-host row for this address (compared case-insensitively), or None."""
    conn = db if db is not None else get_db()
    row = conn.execute(
        "SELECT id FROM connected_accounts WHERE provider='mailhost' AND lower(address)=lower(?)", (address.strip(),),
    ).fetchone()
    return str(row["id"]) if row is not None else None
