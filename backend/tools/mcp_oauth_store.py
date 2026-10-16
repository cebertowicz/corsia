"""Where an MCP server's OAuth sign-in lives: the key store, nowhere else.

One entry per (app, person), ``mcp-oauth-<app slug>-<10 hex of hash(app, person)>``,
holds the whole grant (tokens, expiry, issuer, resource, client registration) as
one base64url JSON value. A refresh rotates the access and refresh token in that
single write, so there is no half-written pair. The entry is created only by a
person's sign-in (:func:`save_new`); a refresh can only replace an entry that
already exists (:func:`rotate`). Entries have no label, so Settings > Keys never
lists them. Errors carry fixed text and never a value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field, fields

from infra import key_store
from infra.key_store_mcp import OAUTH_PREFIX, OAUTH_VALUE_MAX, is_oauth_entry_name
import tools.mcp_oauth_secrets as mcp_oauth_secrets

_SLUG = 10
_HASH = 10


class GrantStoreError(RuntimeError):
    """The grant could not be read or written. The text is fixed; it never holds a value."""


@dataclass
class Grant:
    app_id: str
    user_id: str
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


def _slug(app_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", app_id.lower()).strip("-")[:_SLUG].strip("-") or "x"


def entry_name(app_id: str, user_id: str) -> str:
    digest = hashlib.sha256(f"oauth\0{app_id}\0{user_id}".encode()).hexdigest()[:_HASH]
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


def register_secrets(grant: Grant) -> None:
    """Feed every secret in ``grant`` to the redactor before it is used anywhere."""
    mcp_oauth_secrets.register(*grant.secrets())


def save_new(grant: Grant, *, db: sqlite3.Connection | None = None) -> None:
    """Keep a new sign-in. Only a person's completed sign-in calls this."""
    register_secrets(grant)
    try:
        key_store.set_managed_value(entry_name(grant.app_id, grant.user_id), _encode(grant), db=db)
    except key_store.KeyStoreError:
        raise GrantStoreError("the sign-in could not be kept") from None


def rotate(grant: Grant, *, db: sqlite3.Connection | None = None) -> None:
    """Replace the tokens of an existing sign-in in one write. Never creates a grant."""
    register_secrets(grant)
    name = entry_name(grant.app_id, grant.user_id)
    if name not in key_store.resolve_names([name], db=db):
        raise GrantStoreError("there is no sign-in to refresh")
    try:
        key_store.set_managed_value(name, _encode(grant), db=db)
    except key_store.KeyStoreError:
        raise GrantStoreError("the refreshed sign-in could not be kept") from None


def _entries_for(app_id: str, db: sqlite3.Connection) -> list[Grant]:
    like = f"{OAUTH_PREFIX}{_slug(app_id)}-%"
    names = [
        row["name"] for row in db.execute("SELECT name FROM key_store_entries WHERE name LIKE ?", (like,))
        if is_oauth_entry_name(row["name"])
    ]
    grants = []
    for value in key_store.resolve_names(names, db=db).values():
        grant = _decode(value)
        if grant is not None and grant.app_id == app_id:
            register_secrets(grant)
            grants.append(grant)
    return grants


def load(app_id: str, user_id: str | None = None, *, db: sqlite3.Connection | None = None) -> Grant | None:
    """The sign-in for ``app_id`` (for ``user_id`` when given; else the only one that exists).

    Without a user, two people's sign-ins are ambiguous, so neither is used: the app reads as
    signed out rather than acting as whichever person's grant came first.
    """
    conn = db if db is not None else key_store._db(None)
    try:
        if user_id is not None:
            value = key_store.resolve_names([entry_name(app_id, user_id)], db=conn).get(entry_name(app_id, user_id))
            grant = _decode(value) if value else None
            if grant is not None:
                register_secrets(grant)
            return grant
        grants = _entries_for(app_id, conn)
        return grants[0] if len(grants) == 1 else None
    finally:
        if db is None:
            conn.close()


def load_all(app_id: str, *, db: sqlite3.Connection | None = None) -> list[Grant]:
    """Every person's sign-in for ``app_id``; for revoking them all when the app goes."""
    conn = db if db is not None else key_store._db(None)
    try:
        return _entries_for(app_id, conn)
    finally:
        if db is None:
            conn.close()


def delete_all(app_id: str, *, db: sqlite3.Connection | None = None) -> int:
    """Delete every sign-in entry of ``app_id``, whatever else failed. Returns how many."""
    conn = db if db is not None else key_store._db(None)
    try:
        like = f"{OAUTH_PREFIX}{_slug(app_id)}-%"
        names = [
            row["name"] for row in conn.execute("SELECT name FROM key_store_entries WHERE name LIKE ?", (like,))
            if is_oauth_entry_name(row["name"])
        ]
        # Only entries whose content names this app are deleted; a slug can be shared.
        mine = [name for name in names if _owned_by(name, app_id, conn)]
        for name in mine:
            key_store.drop_managed_entry(name, db=conn)
        return len(mine)
    finally:
        if db is None:
            conn.close()


def _owned_by(name: str, app_id: str, conn: sqlite3.Connection) -> bool:
    value = key_store.resolve_names([name], db=conn).get(name)
    grant = _decode(value) if value else None
    return grant is not None and grant.app_id == app_id


def register_stored(*, db: sqlite3.Connection | None = None) -> int:
    """Register every stored token with the redactor (startup). Returns how many grants."""
    conn = db if db is not None else key_store._db(None)
    try:
        names = [
            row["name"] for row in conn.execute("SELECT name FROM key_store_entries WHERE name LIKE ?", (f"{OAUTH_PREFIX}%",))
            if is_oauth_entry_name(row["name"])
        ]
        count = 0
        for value in key_store.resolve_names(names, db=conn).values():
            grant = _decode(value)
            if grant is not None:
                register_secrets(grant)
                count += 1
        return count
    finally:
        if db is None:
            conn.close()
