"""Where a hosted app's OAuth sign-in lives: the key store, nowhere else.

A sign-in belongs to the connected app, like the app's key: every agent the app
reaches uses it. One entry per app, ``mcp-oauth--<app slug>-<10 hex of hash(app)>``,
holds the whole grant (tokens, expiry, issuer, resource, client registration, and
which person signed in, for the audit trail) as one base64url JSON value. A
refresh rotates the access and refresh token in that single write, so there is no
half-written pair. Only a person's completed sign-in creates or replaces the
entry (:func:`replace`); a refresh can only rewrite one that exists
(:func:`rotate`). Every writer holds :func:`grant_lock`. Entries have no label, so
Settings > Keys never lists them; they end with the app (:func:`end`). Errors
carry fixed text and never a value. Reading a grant never writes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field, fields

from infra import key_store
from infra.database import get_db
from infra.key_store_mcp import OAUTH_PREFIX, OAUTH_VALUE_MAX, is_oauth_entry_name
from tools.mcp_config import url_origin
import tools.mcp_oauth_secrets as mcp_oauth_secrets

_SLUG = 10
_HASH = 10
_locks: dict[str, asyncio.Lock] = {}


class GrantStoreError(RuntimeError):
    """The grant could not be read or written. The text is fixed; it never holds a value."""


@dataclass
class Grant:
    app_id: str
    #: The CORSIA person who signed in. For the audit trail; the row shows the provider's ``account``.
    signed_in_by: str
    server_url: str
    issuer: str
    resource: str
    token_endpoint: str
    client_id: str
    access_token: str
    expires_at: float
    revocation_endpoint: str | None = None
    client_secret: str | None = None
    registration_access_token: str | None = None
    refresh_token: str | None = None
    scope: str | None = None
    account: str | None = None
    signed_in_at: float = field(default_factory=time.time)
    last_refreshed_at: float | None = None
    refresh_count: int = 0

    def secrets(self) -> tuple[str | None, ...]:
        return (self.access_token, self.refresh_token, self.client_secret, self.registration_access_token)

    def issued_for(self, url: str) -> bool:
        """Whether this grant was issued for ``url``'s origin. It is never sent, refreshed or shown for another."""
        origin = url_origin(url)
        return origin is not None and url_origin(self.server_url) == origin and (
            not self.resource or url_origin(self.resource) == origin
        )


def grant_lock(app_id: str) -> asyncio.Lock:
    """The one lock every writer of ``app_id``'s grant holds, so a rotated refresh token is never overwritten."""
    return _locks.setdefault(app_id, asyncio.Lock())


def _slug(app_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", app_id.lower()).strip("-")[:_SLUG].strip("-") or "x"


def entry_name(app_id: str) -> str:
    digest = hashlib.sha256(f"oauth\0{app_id}".encode()).hexdigest()[:_HASH]
    return f"{OAUTH_PREFIX}{_slug(app_id)}-{digest}"


def _encode(grant: Grant) -> str:
    raw = json.dumps(asdict(grant), separators=(",", ":"), sort_keys=True).encode()
    value = base64.urlsafe_b64encode(raw).decode()
    if len(value) > OAUTH_VALUE_MAX:
        raise GrantStoreError("the sign-in is too large to keep")
    return value


def _decode(value: str) -> Grant | None:
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode()))
        names = {item.name for item in fields(Grant)}
        return Grant(**{key: data[key] for key in data if key in names})
    except (ValueError, TypeError, KeyError):
        return None


def _hold(grant: Grant) -> None:
    mcp_oauth_secrets.hold(grant.app_id, *grant.secrets())


def _write(grant: Grant, conn: sqlite3.Connection, failure: str) -> None:
    _hold(grant)
    try:
        key_store.set_managed_value(entry_name(grant.app_id), _encode(grant), db=conn)
    except key_store.KeyStoreError:
        raise GrantStoreError(failure) from None


def _read(name: str, conn: sqlite3.Connection) -> Grant | None:
    value = key_store.resolve_names([name], db=conn).get(name)
    return _decode(value) if value else None


def load(app_id: str, *, db: sqlite3.Connection | None = None) -> Grant | None:
    """The app's sign-in, or None when it has none."""
    conn = db if db is not None else get_db()
    grant = _read(entry_name(app_id), conn)
    if grant is not None:
        _hold(grant)
    return grant


def replace(grant: Grant, *, db: sqlite3.Connection | None = None) -> Grant | None:
    """Keep a completed sign-in as the app's grant, returning the one it replaced. Only a person's sign-in calls this."""
    conn = db if db is not None else get_db()
    previous = load(grant.app_id, db=conn)
    _write(grant, conn, "the sign-in could not be kept")
    return previous


def rotate(grant: Grant, *, db: sqlite3.Connection | None = None) -> None:
    """Replace the tokens of the app's existing sign-in in one write. Never creates a grant."""
    conn = db if db is not None else get_db()
    name = entry_name(grant.app_id)
    if name not in key_store.resolve_names([name], db=conn):
        raise GrantStoreError("there is no sign-in to refresh")
    _write(grant, conn, "the refreshed sign-in could not be kept")


def end(app_id: str, *, db: sqlite3.Connection | None = None) -> Grant | None:
    """Delete the app's sign-in. Returns the grant it ended, for revoking at the provider afterwards."""
    conn = db if db is not None else get_db()
    grant = load(app_id, db=conn)
    key_store.drop_managed_entry(entry_name(app_id), db=conn)
    mcp_oauth_secrets.release(app_id)
    return grant


def register_stored(*, db: sqlite3.Connection | None = None) -> int:
    """Hold every stored grant's tokens for redaction (startup). Returns how many grants."""
    conn = db if db is not None else get_db()
    names = [
        row["name"] for row in conn.execute("SELECT name FROM key_store_entries WHERE name LIKE ?", (f"{OAUTH_PREFIX}%",))
        if is_oauth_entry_name(row["name"])
    ]
    count = 0
    for name, value in key_store.resolve_names(names, db=conn).items():
        grant = _decode(value)
        if grant is None or name != entry_name(grant.app_id):
            continue
        _hold(grant)
        count += 1
    return count
