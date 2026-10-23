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
    store.WAITING: "waiting", store.CHECKING: "checking", store.REFUSED: "refused",
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


def _key_refs(account_id: str, db: sqlite3.Connection) -> list[dict[str, Any]]:
    from connections.connected_accounts import MAILHOST_PROVIDER, credential_key

    entry = credential_key(account_id, MAILHOST_PROVIDER)
    row = db.execute("SELECT label FROM key_store_entries WHERE name=?", (entry,)).fetchone()
    stored = key_store.resolve_names([entry], db=db)
    return [{"entry": entry, "name": row["label"] if row and row["label"] else entry, "set": entry in stored}]


def row_detail(account_id: str, db: sqlite3.Connection) -> dict[str, Any]:
    """The extra fields of one mail-host list row: its host label and ``mailhost`` (the problem and servers)."""
    endpoints, check = store.read(account_id, db=db)
    if endpoints is None:
        return {}
    status = check.get("status", store.WAITING)
    problem = _PROBLEM.get(status)
    host = str(check.get("host") or "")
    detail: dict[str, Any] = {
        "problem": problem, "imap": server_text(endpoints.imap), "smtp": server_text(endpoints.smtp),
        "caldav": endpoints.caldav or "", "key_refs": _key_refs(account_id, db),
        "calendar_missing": status == store.MAIL_ONLY,
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
