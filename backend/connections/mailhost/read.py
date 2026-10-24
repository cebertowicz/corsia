"""Search and read one mail-host account's inbox: headers on search, a decoded body on read.

A message is named ``<uidvalidity>-<uid>``, so an id from before the host rebuilt its mailbox
can't point at a different message: it reads as not found. Mail is outside content; it is
parsed with the standard library's ``email`` policy and nothing of a host's reply reaches a
caller (H15). A host that stops accepting the saved password turns the row to App password
not accepted, so agents stop using it (H13).
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from connections.account_errors import AccountError
from connections.connected_accounts import resolve_credentials
from connections.mailhost import imap, store
from connections.mailhost.endpoints import Endpoints
from connections.mailhost.errors import PASSWORD_REFUSED, MailHostError
from connections.mailhost.provider import PASSWORD
from connections.mailhost.query import QueryRefused, criteria
from infra.database import get_db
from infra.db_async import run_in_db
from infra.state.ws import notify_connected_apps_updated

log = logging.getLogger(__name__)

MAX_RESULTS = 25
MAX_FETCH_BYTES = 500_000
HEADER_NAMES = ("from", "to", "cc", "date", "subject")
HEADER_CHARS = 500
NOT_FOUND = "That message can't be found."
NEEDS_PASSWORD = "This email account needs an app password. Ask the person to set it on its row in Connected apps."
_ID = re.compile(r"^(\d{1,12})-(\d{1,12})$")
_TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)
_BLANKS = re.compile(r"\n{3,}")


class MessageNotFound(AccountError):
    """The message id names nothing in this mailbox; the text is safe to show."""


@dataclass(frozen=True)
class Message:
    message_id: str
    thread_id: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


def parse_message_id(value: object) -> tuple[int, int]:
    """``(uidvalidity, uid)`` from a message id, or a fixed refusal that keeps model text out of IMAP commands."""
    found = _ID.match(value.strip()) if isinstance(value, str) else None
    if found is None:
        raise MessageNotFound(NOT_FOUND)
    return int(found.group(1)), int(found.group(2))


def check_message_id(value: object) -> str:
    """The message id in its one canonical form, or a fixed refusal."""
    return "{}-{}".format(*parse_message_id(value))


def _headers(msg: email.message.EmailMessage) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in HEADER_NAMES:
        try:
            value = " ".join(str(msg.get(name, "")).split())
        except Exception:  # noqa: BLE001 - a malformed header is left out, not fatal
            value = ""
        if value:
            found[name] = value[:HEADER_CHARS]
    return found


def _text_of(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError, UnicodeError):
        return ""
    if part.get_content_type() == "text/html":
        return _BLANKS.sub("\n\n", html.unescape(_TAGS.sub(" ", text))).strip()
    return text.strip()


def parse(raw: bytes, *, with_body: bool) -> tuple[dict[str, str], str]:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return _headers(msg), _text_of(msg) if with_body else ""  # type: ignore[arg-type]


def credentials(account_id: str) -> tuple[Endpoints, str, str, dict[str, object]]:
    """``(settings, username, app password, check)`` for one connected account."""
    db = get_db()
    try:
        endpoints, check = store.read(account_id, db=db)
        bundle = resolve_credentials(account_id, db=db)
    finally:
        db.close()
    if endpoints is None or not bundle:
        raise AccountError(NEEDS_PASSWORD)
    return endpoints, endpoints.imap_username or endpoints.username, str(bundle[PASSWORD]), check


def _demote(account_id: str, check: dict[str, object]) -> bool:
    """Demote a row only when the failing credential snapshot is still current."""
    return store.demote_revoked(account_id, check)


async def _guarded(account_id: str, check: dict[str, object], call):  # type: ignore[no-untyped-def]
    try:
        return await asyncio.to_thread(call)
    except MailHostError as exc:
        if exc.cause == PASSWORD_REFUSED:
            if await run_in_db(_demote, account_id, check):
                notify_connected_apps_updated()
        raise


async def search(account_id: str, query: str, max_results: int) -> list[Message]:
    """The newest matches, newest first, headers only."""
    try:
        wanted = criteria(query, today=date.today())
    except QueryRefused as exc:
        raise AccountError(str(exc)) from None
    endpoints, username, password, check = await run_in_db(credentials, account_id)
    limit = max(1, min(int(max_results), MAX_RESULTS))

    def call() -> tuple[int, list[tuple[int, bytes]]]:
        with imap.account_lock(account_id):
            return imap.search(endpoints, username, password, wanted, limit)

    validity, found = await _guarded(account_id, check, call)
    return [Message(f"{validity}-{uid}", "", parse(raw, with_body=False)[0]) for uid, raw in found]


async def fetch(account_id: str, message_id: str) -> Message:
    """One message with its headers and decoded body."""
    validity, uid = parse_message_id(message_id)
    endpoints, username, password, check = await run_in_db(credentials, account_id)

    def call() -> bytes | None:
        with imap.account_lock(account_id):
            return imap.fetch(endpoints, username, password, validity, uid, MAX_FETCH_BYTES)

    raw = await _guarded(account_id, check, call)
    if raw is None:
        raise MessageNotFound(NOT_FOUND)
    headers, body = parse(raw, with_body=True)
    return Message(f"{validity}-{uid}", "", headers, body)
