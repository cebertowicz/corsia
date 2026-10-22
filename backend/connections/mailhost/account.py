"""Adding a mail-host account: one row per address, its settings, and the Keys entry for its app password.

The agent supplies an address and, only for a host CORSIA doesn't know, server names. A known
host's settings come from the table alone, and an address that already has a row gets that
row back with its servers untouched: a server name decides where the person's password goes,
so neither an agent nor a second call can redirect it (G3). Refusal texts are fixed.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from connections.account_state import emit_lifecycle_event, record_audit
from connections.connected_accounts import MAILHOST_PROVIDER, create_account_for_thread, credential_key, get_account
from connections.mailhost import store
from connections.mailhost.endpoints import (
    Endpoints, EndpointsRefused, domain_of, parse_address, parse_caldav, parse_server,
)
from connections.mailhost.known_hosts import known_host
from infra import key_store
from infra.database import write_transaction

GOOGLE_DOMAINS = frozenset(("gmail.com", "googlemail.com"))
MICROSOFT_DOMAINS = frozenset(("outlook.com", "hotmail.com", "live.com", "msn.com"))
USE_GOOGLE = "That is a Gmail address. Use google_connect for it, not mail_host_connect."
USE_MICROSOFT = "That is a Microsoft address. Use microsoft_connect for it, not mail_host_connect."
UNKNOWN_HOST = (
    "CORSIA doesn't know this address's mail servers. Ask the person for their IMAP and SMTP server names "
    "(from their IT or their host's help page), then call mail_host_connect again with imap and smtp."
)
KEY_DESCRIPTION = "App password for this email account."
KEY_LABEL_MAX = key_store.LABEL_MAX


def _endpoints(address: str, imap: object, smtp: object, caldav: object, username: object) -> Endpoints:
    """The settings for a new row: the known-host table for a known host, else what was typed in."""
    known = known_host(address)
    if known is not None:
        return known.endpoints(address)
    if imap is None or smtp is None:
        raise EndpointsRefused(UNKNOWN_HOST)
    typed_name = parse_address(username) if username else address
    return Endpoints(
        name=domain_of(address), known=False, username=typed_name,
        imap=parse_server(imap, field_name="imap"), smtp=parse_server(smtp, field_name="smtp"),
        caldav=parse_caldav(caldav) if caldav else None,
    )


def key_label(endpoints: Endpoints, address: str) -> str:
    """``<host> key: <address>``; an address too long to show whole falls back to ``<host> key``."""
    label = f"{endpoints.name} key: {address}"
    return label if len(label) <= KEY_LABEL_MAX else f"{endpoints.name} key"


def describe(account_id: str, *, db: sqlite3.Connection, existing: bool) -> dict[str, Any]:
    """What the agent may know about a row: never a credential, never a host's reply text."""
    account = get_account(account_id, db=db)
    endpoints, check = store.read(account_id, db=db)
    if endpoints is None:
        raise EndpointsRefused(UNKNOWN_HOST)
    described: dict[str, Any] = {
        "account_id": account_id, "address": account["address"], "host": endpoints.name,
        "status": check.get("status", store.WAITING), "already_existed": existing,
        "app_password_url": endpoints.app_password_url, "click_path": endpoints.click_path,
        "key_name": account["credential_key"],
    }
    if not endpoints.known:
        described["password_goes_to"] = endpoints.password_destinations()
    if endpoints.enable_imap_step:
        described["first_turn_on_imap"] = endpoints.enable_imap_step
    return described


def connect(
    address: object, *, thread_id: str, imap: object = None, smtp: object = None, caldav: object = None,
    username: object = None, db: sqlite3.Connection,
) -> dict[str, Any]:
    """Add the address's row and Keys entry, or return the row it already has unchanged."""
    clean = parse_address(address)
    if domain_of(clean) in GOOGLE_DOMAINS:
        raise EndpointsRefused(USE_GOOGLE)
    if domain_of(clean) in MICROSOFT_DOMAINS:
        raise EndpointsRefused(USE_MICROSOFT)
    with write_transaction(db, scope="mail_host_connect") as conn:
        found = store.row_exists_for_address(clean, db=conn)
        if found is not None:
            return describe(found, db=conn, existing=True)
        endpoints = _endpoints(clean, imap, smtp, caldav, username)
        account = create_account_for_thread(
            MAILHOST_PROVIDER, clean, actor_thread_id=thread_id, created_by=("agent", thread_id),
            provider_subject=clean.lower(), state="needs_sign_in", db=conn,
        )
        account_id = str(account["id"])
        store.write(account_id, endpoints, status=store.WAITING, db=conn)
        key_store.ensure_managed_entry(credential_key(account_id, MAILHOST_PROVIDER), key_label(endpoints, clean),
                                       KEY_DESCRIPTION, db=conn)
        record_audit(account_id, "connect", ("agent", thread_id), db=conn)
        result = describe(account_id, db=conn, existing=False)
    emit_lifecycle_event("connect_started", account_id, MAILHOST_PROVIDER)
    return result
