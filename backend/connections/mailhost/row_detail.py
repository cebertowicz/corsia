"""What a mail-host account's Connected apps row says about its last check.

Built from the row's stored settings and check result only: a server name, a status the check
named, the servers as ``host[:port]``. Never a credential, a key value or a host's reply text (H14, H15).
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import urlsplit

from connections.mailhost import store
from connections.mailhost.endpoints import Endpoints, Server
from connections.mailhost.known_hosts import known_host
from infra import key_store

_PROBLEM = {
    store.WAITING: "waiting", store.CHECKING: "checking", store.REFUSED: "refused", store.REVOKED: "revoked",
    store.IMAP_OFF: "imap_off", store.UNREACHABLE: "unreachable",
}


def server_text(server: Server) -> str:
    return f"{server.host}:{server.port}" if server.port else server.host


def _failing(endpoints: Endpoints, host: str) -> str | None:
    """The server field a failed check named, so the open Edit server form can mark it."""
    if host and host == endpoints.imap.host:
        return "imap"
    if host and host == endpoints.smtp.host:
        return "smtp"
    if host and endpoints.caldav and host == urlsplit(endpoints.caldav).hostname:
        return "caldav"
    return None



def _detail(account_id: str, endpoints: Endpoints, check: dict[str, Any], key_ref: dict[str, Any]) -> dict[str, Any]:
    status = check.get("status", store.WAITING)
    problem = _PROBLEM.get(status)
    host = str(check.get("host") or "")
    detail: dict[str, Any] = {
        "problem": problem, "imap": server_text(endpoints.imap), "smtp": server_text(endpoints.smtp),
        "caldav": endpoints.caldav or "", "key_refs": [key_ref], "calendar_missing": status == store.MAIL_ONLY,
    }
    if problem == "unreachable":
        detail["server"] = host or endpoints.imap.host
        failing = _failing(endpoints, host)
        if failing:
            detail["failing"] = failing
    known = known_host(endpoints.username) if endpoints.known else None
    if endpoints.app_password_url and (known is None or known.shows_account_link):
        detail["manage_url"] = endpoints.app_password_url
    if not endpoints.known:
        detail["destination_hosts"] = endpoints.password_destinations()
    elif known and known.product:
        detail["product_name"] = known.product
    return {"provider_display_name": endpoints.name, "mailhost": detail}


def row_details(account_ids: list[str], db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Mail-host row details in three queries regardless of how many rows are present."""
    if not account_ids:
        return {}
    from connections.connected_accounts import MAILHOST_PROVIDER, credential_key

    marks = ",".join("?" for _ in account_ids)
    rows = db.execute(
        f"SELECT id,endpoints_json FROM connected_accounts WHERE provider='mailhost' AND id IN ({marks})", account_ids,
    ).fetchall()
    by_id = {str(row["id"]): store._read(row) for row in rows}
    entries = {account_id: credential_key(account_id, MAILHOST_PROVIDER) for account_id in by_id}
    key_names = list(entries.values())
    key_marks = ",".join("?" for _ in key_names)
    labels = {
        str(row["name"]): str(row["label"] or row["name"])
        for row in db.execute(f"SELECT name,label FROM key_store_entries WHERE name IN ({key_marks})", key_names)
    }
    stored = key_store.resolve_names(key_names, db=db)
    result: dict[str, dict[str, Any]] = {}
    for account_id, (endpoints, check) in by_id.items():
        if endpoints is None:
            continue
        entry = entries[account_id]
        result[account_id] = _detail(
            account_id, endpoints, check,
            {"entry": entry, "name": labels.get(entry, entry), "set": entry in stored},
        )
    return result


def destination_hosts_for_keys(db: sqlite3.Connection | None = None) -> dict[str, list[str]]:
    """Destination disclosure hosts keyed by mail-host credential entry, in one row read."""
    from connections.connected_accounts import MAILHOST_PROVIDER, credential_key
    from infra.database import get_db

    conn = db or get_db()
    try:
        result: dict[str, list[str]] = {}
        for row in conn.execute("SELECT id,endpoints_json FROM connected_accounts WHERE provider='mailhost'"):
            endpoints, _check = store._read(row)
            if endpoints is not None and not endpoints.known:
                result[credential_key(str(row["id"]), MAILHOST_PROVIDER)] = endpoints.password_destinations()
        return result
    finally:
        if db is None:
            conn.close()


def row_detail(account_id: str, db: sqlite3.Connection) -> dict[str, Any]:
    """The extra fields of one mail-host list row."""
    return row_details([account_id], db).get(account_id, {})
