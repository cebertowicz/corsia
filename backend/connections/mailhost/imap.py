"""Reading a mailbox over IMAP, in worker threads, over TLS or not at all.

Login happens only after TLS is up: implicit TLS from the first byte, or STARTTLS that the
server offered and completed. A server without STARTTLS on a STARTTLS port is unreachable and
never sees the password. A host's reply is read only to name a cause; its text goes no
further (it can echo what was sent), and ``imaplib`` debug output is never enabled.
"""

from __future__ import annotations

import imaplib
import re
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from connections.mailhost import folders, tls
from connections.mailhost.endpoints import Endpoints
from connections.mailhost.errors import IMAP_OFF, PASSWORD_REFUSED, SERVER_UNREACHABLE, MailHostError

TIMEOUT_SECONDS = 20.0
MAILBOX = "INBOX"
_TRANSPORT = (OSError, EOFError, ssl.SSLError, socket.timeout, imaplib.IMAP4.error)
_IMAP_OFF = re.compile(r"imap.{0,40}(disabled|not enabled|turned off|not allowed|not activated)|disabled.{0,40}imap", re.I)
_TEMPORARY = re.compile(r"\[(?:UNAVAILABLE|SERVERBUG|LIMIT|INUSE)\]|too many connections|try again later", re.I)
_FETCHED = re.compile(rb"UID (\d+)")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def account_lock(account_id: str) -> threading.Lock:
    """At most one IMAP connection per account at a time."""
    with _locks_guard:
        return _locks.setdefault(account_id, threading.Lock())


def _unreachable(ep: Endpoints) -> MailHostError:
    return MailHostError(SERVER_UNREACHABLE, host=ep.imap.host)


def _open(ep: Endpoints) -> imaplib.IMAP4:
    """A connection with TLS up and no credential sent yet."""
    host, port = ep.imap.host, ep.imap_port()
    try:
        if ep.imap.tls_starts_with_starttls(imap=True):
            conn = imaplib.IMAP4(host, port, timeout=TIMEOUT_SECONDS)
            if "STARTTLS" not in conn.capabilities:
                conn.shutdown()
                raise _unreachable(ep)
            conn.starttls(ssl_context=tls.context())
            return conn
        return imaplib.IMAP4_SSL(host, port, ssl_context=tls.context(), timeout=TIMEOUT_SECONDS)
    except MailHostError:
        raise
    except _TRANSPORT:
        raise _unreachable(ep) from None


def _login_failure(ep: Endpoints, reply: object) -> MailHostError:
    """Name why a host refused LOGIN; the reply is inspected here and dropped."""
    text = " ".join(
        part.decode("utf-8", "replace") if isinstance(part, bytes) else str(part)
        for part in (reply if isinstance(reply, tuple) else (reply,))
    )
    if _TEMPORARY.search(text):
        return _unreachable(ep)
    if _IMAP_OFF.search(text):
        return MailHostError(IMAP_OFF, name=ep.name)
    return MailHostError(PASSWORD_REFUSED, name=ep.name)


@contextmanager
def session(ep: Endpoints, username: str, password: str) -> Iterator[imaplib.IMAP4]:
    """A logged-in connection, always logged out and closed."""
    conn = _open(ep)
    try:
        try:
            conn.login(username, password)
        except imaplib.IMAP4.abort:
            raise _unreachable(ep) from None
        except imaplib.IMAP4.error as exc:
            raise _login_failure(ep, exc.args) from None
        except _TRANSPORT:
            raise _unreachable(ep) from None
        yield conn
    except (OSError, EOFError, ssl.SSLError, imaplib.IMAP4.error):
        raise _unreachable(ep) from None
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - closing a dead connection has nothing left to report
            pass


def check_login(ep: Endpoints, password: str) -> str:
    """The username form the host accepted; a refusal of every form is a refused password."""
    refused: MailHostError | None = None
    for username in ep.usernames(ep.imap_username):
        try:
            with session(ep, username, password):
                return username
        except MailHostError as exc:
            if exc.cause != PASSWORD_REFUSED:
                raise
            refused = exc
    raise refused or MailHostError(PASSWORD_REFUSED, name=ep.name)


def _select(conn: imaplib.IMAP4, ep: Endpoints) -> int:
    """Open the inbox read-only; return its UIDVALIDITY."""
    status, _ = conn.select(MAILBOX, readonly=True)
    validity = conn.response("UIDVALIDITY")[1]
    if status != "OK" or not validity or not validity[0]:
        raise _unreachable(ep)
    return int(validity[0])


def _items(data: list) -> Iterator[tuple[int, bytes]]:
    """``(uid, literal bytes)`` for every message a FETCH answered."""
    for index, part in enumerate(data):
        if isinstance(part, tuple):
            # A server may send the UID before the literal or after it.
            after = data[index + 1] if index + 1 < len(data) and isinstance(data[index + 1], bytes) else b""
            found = _FETCHED.search(part[0]) or _FETCHED.search(after)
            if found:
                yield int(found.group(1)), part[1]


def search(ep: Endpoints, username: str, password: str, criteria: list[str], limit: int) -> tuple[int, list[tuple[int, bytes]]]:
    """``(uidvalidity, [(uid, header bytes)])`` for the newest matches in the inbox, newest first."""
    with session(ep, username, password) as conn:
        validity = _select(conn, ep)
        status, found = conn.uid("SEARCH", *criteria)
        if status != "OK":
            raise _unreachable(ep)
        uids = (found[0] or b"").split()[-limit:][::-1]
        if not uids:
            return validity, []
        status, data = conn.uid("FETCH", b",".join(uids).decode(), "(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE SUBJECT)])")
        if status != "OK":
            raise _unreachable(ep)
        by_uid = dict(_items(data))
        return validity, [(int(uid), by_uid[int(uid)]) for uid in uids if int(uid) in by_uid]


def _quoted(folder: str) -> str:
    """imaplib sends an APPEND mailbox name as given, so a name with a space needs its quotes."""
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def append(ep: Endpoints, username: str, password: str, kind: str, raw: bytes) -> bool:
    """Save a reviewed RFC 822 message in the host's Drafts or Sent folder; False when the host shows no such folder."""
    with session(ep, username, password) as conn:
        key = (f"{ep.imap.host}:{ep.imap_port()}", username)
        folder = folders.resolve(conn, key, kind)
        if folder is None:
            return False
        status, _ = conn.append(_quoted(folder), None, None, raw)
        if status != "OK":
            folders.forget(key)
            raise _unreachable(ep)
        return True


def reply_headers(ep: Endpoints, username: str, password: str, validity: int, uid: int) -> bytes | None:
    """The threading and address headers of one inbox message, or None when it is gone or its mailbox was rebuilt."""
    fields = "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID REFERENCES REPLY-TO FROM SUBJECT)])"
    with session(ep, username, password) as conn:
        if _select(conn, ep) != validity:
            return None
        status, data = conn.uid("FETCH", str(uid), fields)
        if status != "OK":
            raise _unreachable(ep)
        return dict(_items(data)).get(uid)


def _last_uid(conn: imaplib.IMAP4, ep: Endpoints) -> int:
    status, found = conn.uid("SEARCH", "ALL")
    if status != "OK":
        raise _unreachable(ep)
    return max((int(uid) for uid in (found[0] or b"").split()), default=0)


def inbox_cursor(ep: Endpoints, username: str, password: str) -> tuple[int, int]:
    """The inbox UIDVALIDITY and current last UID, used as a no-replay poll floor."""
    with session(ep, username, password) as conn:
        validity = _select(conn, ep)
        return validity, _last_uid(conn, ep)


POLL_BATCH_SIZE = 50


def added_after(
    ep: Endpoints, username: str, password: str, validity: int, last_uid: int, max_bytes: int,
) -> tuple[int, int, list[tuple[int, bytes]]]:
    """Fetch one bounded, oldest-first batch newer than the durable UID cursor."""
    with session(ep, username, password) as conn:
        current_validity = _select(conn, ep)
        if current_validity != validity:
            # A rebuilt mailbox: the old cursor means nothing here, so take the whole mailbox's
            # last UID as the new no-replay floor.  Rare, so the full search is acceptable.
            return current_validity, _last_uid(conn, ep), []
        status, found = conn.uid("SEARCH", "UID", f"{last_uid + 1}:*")
        if status != "OK":
            raise _unreachable(ep)
        # RFC 3501 permits a range starting beyond the current highest UID to include
        # that highest UID, so enforce the durable cursor again client-side.
        wanted = sorted(uid for uid in (int(uid) for uid in (found[0] or b"").split()) if uid > last_uid)[:POLL_BATCH_SIZE]
        if not wanted:
            return current_validity, last_uid, []
        status, data = conn.uid("FETCH", ",".join(str(uid) for uid in wanted), f"(UID BODY.PEEK[]<0.{max_bytes}>)")
        if status != "OK":
            raise _unreachable(ep)
        by_uid = dict(_items(data))
        return current_validity, wanted[-1], [(uid, by_uid[uid]) for uid in wanted if uid in by_uid]


def fetch(ep: Endpoints, username: str, password: str, validity: int, uid: int, max_bytes: int) -> bytes | None:
    """One inbox message's raw bytes (up to ``max_bytes``), or None when it is gone or its mailbox was rebuilt."""
    with session(ep, username, password) as conn:
        if _select(conn, ep) != validity:
            return None
        status, data = conn.uid("FETCH", str(uid), f"(UID BODY.PEEK[]<0.{max_bytes}>)")
        if status != "OK":
            raise _unreachable(ep)
        return dict(_items(data)).get(uid)
