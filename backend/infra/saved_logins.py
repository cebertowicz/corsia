"""Saved website logins: a site and username anyone may see, and a password only the server reads.

A login's password is a key store value (``infra.key_store_values``): it lives
only in the values file in CORSIA's private state, under the key store entry
``saved-login-<id>``. That entry has no label, so Settings › Keys never lists
it, and ``infra.key_store`` refuses to list, set, clear, remove or resolve it
for anyone. The ``saved_logins`` table (``infra.key_store_schema``) holds what
may be shown: the site, the username, whether the login is out of date, who
saved it and when it was last used.

No agent tool reaches this module, and only the person's own routes
(``api.saved_logins``) list, save and delete logins. :func:`password_for_fill`
is the one reader of a password, for the server's own fill, and
:func:`blanking_values` the one reader of them all, for blanking them out of
what the browser returns to agents. A test pins which modules may name either.
Every view is a :class:`SavedLogin`, which has no password field.

Errors raised here carry fixed text and never a value.
"""

from __future__ import annotations

import hmac
import ipaddress
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass

from infra import key_store_values
from infra.database import get_db, write_transaction
from infra.key_store import LOGIN_PREFIX, VALUE_MAX, is_login_entry_name
from infra.key_store_values import KeyStoreError
from infra.time import utc_now_iso

STATUS_OK = "ok"
STATUS_OUT_OF_DATE = "out_of_date"
#: Longest username kept: the longest email address, a 64-character local part, "@" and a
#: 255-character domain (RFC 3696 section 3), since a username is most often an email address.
USERNAME_MAX = 320
#: Longest host name as text: 255 octets on the wire (RFC 1035 section 2.3.4). With the shape check and a port of at most five
#: digits, it also bounds the whole site address.
_HOST_MAX = 253
#: Set by the output-blanking requirement (C6): saved values under 8 characters are not blanked,
#: since they would match ordinary text in what the browser returns.
BLANKING_MIN = 8

#: ``https://host`` with an optional port and an optional trailing slash, and nothing else:
#: no user, path, query or fragment. The host is a name or IPv4 address, or an IPv6 address in brackets.
_ORIGIN = re.compile(r"(?i:https)://(?P<host>[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::(?P<port>[0-9]{1,5}))?/?")
_HOST_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
_LOGIN_ID = re.compile(r"[0-9a-f]{32}")
#: Unicode control characters (NUL included) and unpaired surrogates, which no page can be typed.
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})

_SITE_REFUSAL = "A saved login's site is an https address, such as https://example.com."
_USERNAME_REFUSAL = f"A username is 1-{USERNAME_MAX} characters with no control characters."
_PASSWORD_EMPTY = "Type the password first."
_PASSWORD_REFUSAL = f"A password is 1-{VALUE_MAX} characters with no control characters. Nothing was saved."
_SAVED_BY_REFUSAL = "A saved login needs the person who saved it."
_THREAD_REFUSAL = "A login's use needs the thread that used it."
_NO_LOGIN = "No saved login has that id."


@dataclass(frozen=True)
class SavedLogin:
    """What may be shown about a saved login. It has no password field, by design."""

    id: str
    site: str
    username: str
    status: str
    created_at: str
    saved_by: str
    last_used_at: str | None
    last_used_by_thread_id: str | None


_COLUMNS = "id, site, username, status, created_at, saved_by, last_used_at, last_used_by_thread_id"


def _db(db: sqlite3.Connection | None) -> sqlite3.Connection:
    return db if db is not None else get_db()


def entry_name(login_id: str) -> str:
    """The key store entry holding this login's password."""
    name = f"{LOGIN_PREFIX}{login_id}"
    if not is_login_entry_name(name):
        raise KeyStoreError(_NO_LOGIN)
    return name


def _host(raw: str) -> str:
    if raw.startswith("["):
        try:
            return f"[{ipaddress.IPv6Address(raw[1:-1]).compressed}]"
        except ValueError:
            raise KeyStoreError(_SITE_REFUSAL) from None
    host = raw.lower()
    labels = host.split(".")
    if len(host) > _HOST_MAX or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise KeyStoreError(_SITE_REFUSAL)
    if all(label.isdigit() for label in labels):
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError:
            raise KeyStoreError(_SITE_REFUSAL) from None
    return host


def normalized_site(site: object) -> str:
    """The https origin a login is saved for: lowercase host, and a port only when it isn't 443."""
    match = _ORIGIN.fullmatch(site) if isinstance(site, str) else None
    if match is None:
        raise KeyStoreError(_SITE_REFUSAL)
    host = _host(match["host"])
    port = int(match["port"]) if match["port"] is not None else 443
    if not 1 <= port <= 65535:
        raise KeyStoreError(_SITE_REFUSAL)
    return f"https://{host}" if port == 443 else f"https://{host}:{port}"


def _no_control(text: str) -> bool:
    return not any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in text)


def validated_username(username: object) -> str:
    """The username exactly as given: 1 to ``USERNAME_MAX`` characters with no control characters."""
    if not isinstance(username, str) or not 1 <= len(username) <= USERNAME_MAX or not _no_control(username):
        raise KeyStoreError(_USERNAME_REFUSAL)
    return username


def validated_password(password: object) -> str:
    """The password exactly as given: any characters but control characters, spaces and Unicode included."""
    if not isinstance(password, str):
        raise KeyStoreError(_PASSWORD_REFUSAL)
    if not password:
        raise KeyStoreError(_PASSWORD_EMPTY)
    if len(password) > VALUE_MAX or not _no_control(password):
        raise KeyStoreError(_PASSWORD_REFUSAL)
    return password


def _login_id(login_id: object) -> str:
    if not isinstance(login_id, str) or not _LOGIN_ID.fullmatch(login_id):
        raise KeyStoreError(_NO_LOGIN)
    return login_id


def save_login(
    *, site: object, username: object, password: object, saved_by: object, db: sqlite3.Connection | None = None,
) -> str:
    """Save a login and return its id. Saving the same site and username again replaces its password.

    A replaced login is marked ok again, and ``saved_by`` names who saved its
    current password; its id and ``created_at`` stay.
    """
    clean_site = normalized_site(site)
    clean_username = validated_username(username)
    clean_password = validated_password(password)
    if not isinstance(saved_by, str) or not saved_by:
        raise KeyStoreError(_SAVED_BY_REFUSAL)
    now = utc_now_iso()
    with write_transaction(db, scope="saved_login_save") as conn:
        row = conn.execute(
            "SELECT id FROM saved_logins WHERE site = ? AND username = ?", (clean_site, clean_username),
        ).fetchone()
        login_id = row[0] if row is not None else uuid.uuid4().hex
        name = entry_name(login_id)
        ref = key_store_values.put(conn, name, clean_password)  # also sweeps the replaced value after commit
        conn.execute(
            "INSERT INTO key_store_entries (name, created_at, set_at, value_ref) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET set_at = excluded.set_at, value_ref = excluded.value_ref",
            (name, now, now, ref),
        )
        conn.execute(
            "INSERT INTO saved_logins (id, site, username, status, saved_by, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status = excluded.status, saved_by = excluded.saved_by",
            (login_id, clean_site, clean_username, STATUS_OK, saved_by, now),
        )
    return login_id


def logins_for_site(site: object, *, db: sqlite3.Connection | None = None) -> list[SavedLogin]:
    """Every login saved for this site, by username. Never a password."""
    rows = _db(db).execute(
        f"SELECT {_COLUMNS} FROM saved_logins WHERE site = ? ORDER BY username", (normalized_site(site),),
    ).fetchall()
    return [SavedLogin(*tuple(row)) for row in rows]


def list_logins(*, db: sqlite3.Connection | None = None) -> list[SavedLogin]:
    """Every saved login, by site and then username. Never a password."""
    rows = _db(db).execute(f"SELECT {_COLUMNS} FROM saved_logins ORDER BY site, username").fetchall()
    return [SavedLogin(*tuple(row)) for row in rows]


def get_login(login_id: object, *, db: sqlite3.Connection | None = None) -> SavedLogin:
    """One login's view, or :class:`KeyStoreError` when there is none. Never a password."""
    row = _db(db).execute(f"SELECT {_COLUMNS} FROM saved_logins WHERE id = ?", (_login_id(login_id),)).fetchone()
    if row is None:
        raise KeyStoreError(_NO_LOGIN)
    return SavedLogin(*tuple(row))


def _update(login_id: object, sql: str, params: tuple, scope: str, db: sqlite3.Connection | None) -> None:
    with write_transaction(db, scope=scope) as conn:
        if conn.execute(sql, (*params, _login_id(login_id))).rowcount == 0:
            raise KeyStoreError(_NO_LOGIN)


def mark_out_of_date(login_id: object, *, db: sqlite3.Connection | None = None) -> None:
    """Mark a login out of date, such as when the site refused it. Saving it again marks it ok."""
    _update(login_id, "UPDATE saved_logins SET status = ? WHERE id = ?", (STATUS_OUT_OF_DATE,), "saved_login_stale", db)


def record_use(login_id: object, thread_id: object, *, db: sqlite3.Connection | None = None) -> None:
    """Record that a thread used this login, and when."""
    if not isinstance(thread_id, str) or not thread_id:
        raise KeyStoreError(_THREAD_REFUSAL)
    _update(
        login_id, "UPDATE saved_logins SET last_used_at = ?, last_used_by_thread_id = ? WHERE id = ?",
        (utc_now_iso(), thread_id), "saved_login_use", db,
    )


def delete_login(login_id: object, *, db: sqlite3.Connection | None = None) -> None:
    """Delete a login; its password leaves the values file in the sweep after the commit."""
    clean_id = _login_id(login_id)
    with write_transaction(db, scope="saved_login_delete") as conn:
        if conn.execute("DELETE FROM saved_logins WHERE id = ?", (clean_id,)).rowcount == 0:
            raise KeyStoreError(_NO_LOGIN)
        conn.execute("DELETE FROM key_store_entries WHERE name = ?", (entry_name(clean_id),))
        key_store_values.sweep_after_commit(conn)


def password_for_fill(login_id: object, *, db: sqlite3.Connection | None = None) -> str:
    """The login's password, for the server to type into a page. The one reader of a single password.

    Only the modules a test names may call it. The value goes from here into
    the page and nowhere else: never a tool result, log, event or audit row.
    """
    name = entry_name(_login_id(login_id))
    value = key_store_values.resolve(_db(db), [name]).get(name)
    if not value:
        raise KeyStoreError(_NO_LOGIN)
    return value


def password_matches(login_id: object, password: object, *, db: sqlite3.Connection | None = None) -> bool:
    """Whether ``password`` is this login's saved password. Answers yes or no and never returns the value.

    For the save offer only, to make no offer when the person signed in with the password already saved.
    A test pins which modules may name it.
    """
    if not isinstance(password, str):
        return False
    name = entry_name(_login_id(login_id))
    saved = key_store_values.resolve(_db(db), [name]).get(name)
    return bool(saved) and hmac.compare_digest(saved.encode("utf-8"), password.encode("utf-8"))


def blanking_values(*, db: sqlite3.Connection | None = None) -> frozenset[str]:
    """Every saved password of ``BLANKING_MIN`` or more characters, to blank wherever the browser returns one."""
    conn = _db(db)
    names = [entry_name(row[0]) for row in conn.execute("SELECT id FROM saved_logins").fetchall()]
    return frozenset(value for value in key_store_values.resolve(conn, names).values() if len(value) >= BLANKING_MIN)
