"""Bounded IMAP metadata reads and lazy evidence reads for the reply guard."""
from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime

from connections.mailhost import folders, imap, read
from infra.db_async import run_in_db
from tools.mail_guard import Message

_IDS = re.compile(r"<[^<>\s]+>")
_FLAGS = re.compile(rb"\bFLAGS\s*\(([^)]*)\)", re.I)
_SENT_ID = re.compile(r"^sent:([0-9a-f]{8}):(\d{1,12}):(\d{1,12})$")
_FIELDS = "FROM DATE SUBJECT REPLY-TO MESSAGE-ID IN-REPLY-TO REFERENCES"


@dataclass
class _Mail:
    folder: str
    uid: int
    validity: int
    parsed: email.message.EmailMessage
    flags: bytes
    own_id: str
    links: set[str]


def _select(conn, ep, folder: str) -> int:
    status, _ = conn.select(imap._quoted(folder), readonly=True)
    validity = conn.response("UIDVALIDITY")[1]
    if status != "OK" or not validity or not validity[0]:
        raise imap._unreachable(ep)
    return int(validity[0])


def _search(conn, ep, *terms: str) -> set[int]:
    status, data = conn.uid("SEARCH", *terms)
    if status != "OK" or not data or data[0] is None:
        raise imap._unreachable(ep)
    try:
        return {int(uid) for uid in data[0].split()}
    except (ValueError, AttributeError):
        raise imap._unreachable(ep) from None


def _batch(conn, ep, uids: set[int], query: str, *, flags: bool) -> dict[int, tuple[bytes, bytes]]:
    if not uids:
        return {}
    status, data = conn.uid("FETCH", ",".join(map(str, sorted(uids))), query)
    if status != "OK":
        raise imap._unreachable(ep)
    found: dict[int, tuple[bytes, bytes]] = {}
    for index, part in enumerate(data):
        if not isinstance(part, tuple) or not isinstance(part[0], bytes) or not isinstance(part[1], bytes):
            continue
        after = data[index + 1] if index + 1 < len(data) and isinstance(data[index + 1], bytes) else b""
        metadata = part[0] + b" " + after
        uid_match = imap._FETCHED.search(metadata)
        flag_match = _FLAGS.search(metadata)
        if uid_match and (flag_match or not flags):
            found[int(uid_match.group(1))] = (part[1], flag_match.group(1) if flag_match else b"")
    # Missing FETCH results are not a clean guard pass (including concurrent expunges).
    if set(found) != uids:
        raise imap._unreachable(ep)
    return found


def _metadata(conn, ep, folder: str, validity: int, uids: set[int]) -> list[_Mail]:
    query = f"(UID ENVELOPE FLAGS BODY.PEEK[HEADER.FIELDS ({_FIELDS})]<0.{read.MAX_FETCH_BYTES}>)"
    found = _batch(conn, ep, uids, query, flags=True)
    result = []
    for uid, (raw, flags) in found.items():
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        own = _IDS.findall(str(msg.get("Message-ID", "")))
        refs = _IDS.findall(" ".join(str(msg.get(name, "")) for name in ("In-Reply-To", "References")))
        own_id = own[0] if own else f"local:{folder}:{validity}:{uid}"
        result.append(_Mail(folder, uid, validity, msg, flags, own_id, set(refs) | {own_id}))
    return result


def _date(msg: email.message.EmailMessage) -> datetime:
    try:
        value = parsedate_to_datetime(str(msg.get("Date", "")))
        if value is None:
            raise ValueError
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("A message has no usable date for the reply guard.") from None


class _SentFolderMissing(ValueError):
    pass


def _sent_folder(conn, ep, username: str) -> str:
    class CheckedList:
        def list(self):
            status, data = conn.list()
            if status != "OK":
                raise imap._unreachable(ep)
            return status, data
    folder = folders.resolve(CheckedList(), imap._folder_key(ep, username), folders.SENT)
    if folder is None:
        raise _SentFolderMissing("Couldn't find the Sent folder.")
    return folder


def _thread_search(ids: set[str]) -> str:
    terms = [f'(HEADER {field} {imap._quoted(mid)})' for mid in sorted(ids)
             for field in ("Message-ID", "References", "In-Reply-To")]
    expression = terms[-1]
    for term in reversed(terms[:-1]):
        expression = f"OR {term} ({expression})"
    return expression


def _thread_ids(messages: list[_Mail]) -> dict[tuple[str, int], str]:
    parent: dict[str, str] = {}
    def root(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            key = parent[key]
        return key
    for mail in messages:
        for link in mail.links:
            a, b = root(mail.own_id), root(link)
            if a != b:
                parent[max(a, b)] = min(a, b)
    return {(mail.folder, mail.uid): root(mail.own_id) for mail in messages}


def _message(account_id: str, mail: _Mail, thread_id: str) -> Message:
    mid = (read.mint_message_id(account_id, mail.validity, mail.uid) if mail.folder == imap.MAILBOX
           else f"sent:{read.account_tag(account_id)}:{mail.validity}:{mail.uid}")
    msg = mail.parsed
    return Message(message_id=mid, thread_id=thread_id, sender=str(msg.get("From", "")),
                   subject=str(msg.get("Subject", "")), date=_date(msg), body="",
                   reply_to=str(msg.get("Reply-To", "")), unread=b"\\seen" not in mail.flags.lower().split())


def _guard_data(ep, username: str, password: str, account_id: str, validity: int, uid: int):
    with imap.account_lock(account_id), imap.session(ep, username, password) as conn:
        if _select(conn, ep, imap.MAILBOX) != validity:
            raise read.MessageNotFound(read.NOT_FOUND)
        # A missing target is distinct from a refused provider read.
        status, data = conn.uid("FETCH", str(uid), f"(UID FLAGS BODY.PEEK[HEADER.FIELDS ({_FIELDS})]<0.{read.MAX_FETCH_BYTES}>)")
        if status != "OK":
            raise imap._unreachable(ep)
        if not list(imap._items(data)):
            raise read.MessageNotFound(read.NOT_FOUND)
        # Parse this response through the same batch parser without another provider call.
        class Response:
            def uid(self, *args):
                return status, data
        target = _metadata(Response(), ep, imap.MAILBOX, validity, {uid})[0]
        if target.own_id.startswith("local:"):
            raise ValueError("The target has no Message-ID for thread reconstruction.")
        address = parseaddr(str(target.parsed.get("From", "")))[1]
        cutoffs = {_date(target.parsed), datetime.now(timezone.utc) - timedelta(days=30)}
        sent_folder = _sent_folder(conn, ep, username)
        folder_names = (imap.MAILBOX, sent_folder)
        validities = {imap.MAILBOX: validity}
        found = {(imap.MAILBOX, uid): target}
        sender_keys: set[tuple[str, int]] = set()
        initial_ids = {link for link in target.links if not link.startswith("local:")}
        for folder in folder_names:
            current = _select(conn, ep, folder)
            if folder == imap.MAILBOX and current != validity:
                raise imap._unreachable(ep)
            validities[folder] = current
            thread_uids = _search(conn, ep, _thread_search(initial_ids))
            sender_uids: set[int] = set()
            if address:
                for cutoff in cutoffs:
                    sender_uids.update(_search(conn, ep, "FROM", imap._quoted(address),
                                               "SINCE", cutoff.strftime("%d-%b-%Y")))
            sender_keys.update((folder, item) for item in sender_uids)
            wanted = (thread_uids | sender_uids) - {item_uid for item_folder, item_uid in found if item_folder == folder}
            for item in _metadata(conn, ep, folder, current, wanted):
                found[(folder, item.uid)] = item
        # A chain that drops full References and exceeds this one extra search round can be missed.
        ids = _thread_ids(list(found.values()))
        root = ids[(imap.MAILBOX, uid)]
        next_ids = {link for item in found.values() if ids[(item.folder, item.uid)] == root
                    for link in item.links if not link.startswith("local:")} - initial_ids
        if next_ids:
            for folder in folder_names:
                if _select(conn, ep, folder) != validities[folder]:
                    raise imap._unreachable(ep)
                wanted = _search(conn, ep, _thread_search(next_ids))
                wanted -= {item_uid for item_folder, item_uid in found if item_folder == folder}
                for item in _metadata(conn, ep, folder, validities[folder], wanted):
                    found[(folder, item.uid)] = item
    ids = _thread_ids(list(found.values()))
    root = ids[(imap.MAILBOX, uid)]
    thread = [_message(account_id, item, ids[key]) for key, item in found.items() if ids[key] == root]
    sender = [_message(account_id, item, ids[key]) for key, item in found.items() if key in sender_keys
              and parseaddr(str(item.parsed.get("From", "")))[1].casefold() == address.casefold()]
    return _message(account_id, target, root), thread, sender


async def guard_data(account_id: str, reply_message_id: str) -> tuple[Message, list[Message], list[Message]]:
    """Return header-only messages. Call fetch_body for the target quote and screened evidence."""
    validity, uid = read.parse_message_id(account_id, reply_message_id)
    ep, username, password, check = await run_in_db(read.credentials, account_id)
    return await read._guarded(account_id, check, lambda: _guard_data(ep, username, password, account_id, validity, uid))


def _body(ep, username: str, password: str, account_id: str, folder_kind: str, validity: int, uid: int) -> str:
    with imap.account_lock(account_id), imap.session(ep, username, password) as conn:
        folder = imap.MAILBOX if folder_kind == "inbox" else _sent_folder(conn, ep, username)
        if _select(conn, ep, folder) != validity:
            raise read.MessageNotFound(read.NOT_FOUND)
        raw = _batch(conn, ep, {uid}, f"(UID BODY.PEEK[]<0.{read.MAX_FETCH_BYTES}>)", flags=False)[uid][0]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return read._text_of(msg)


async def fetch_body(account_id: str, message_id: str) -> str:
    """Read one Inbox or Sent message for quote/evidence; never screen or disclose it here."""
    sent = _SENT_ID.fullmatch(message_id) if isinstance(message_id, str) else None
    if sent:
        if sent.group(1) != read.account_tag(account_id):
            raise read.MessageNotFound(read.NOT_FOUND)
        folder_kind, validity, uid = "sent", int(sent.group(2)), int(sent.group(3))
    else:
        validity, uid = read.parse_message_id(account_id, message_id)
        folder_kind = "inbox"
    ep, username, password, check = await run_in_db(read.credentials, account_id)
    return await read._guarded(account_id, check, lambda: _body(ep, username, password, account_id, folder_kind, validity, uid))


def _sent_html(ep, username: str, password: str, account_id: str) -> list[str]:
    with imap.account_lock(account_id), imap.session(ep, username, password) as conn:
        try:
            folder = _sent_folder(conn, ep, username)
        except _SentFolderMissing:
            # A successful folder listing with no Sent folder has no samples.
            return []
        _select(conn, ep, folder)
        uids = set(sorted(_search(conn, ep, "ALL"), reverse=True)[:5])
        found = _batch(conn, ep, uids, f"(UID BODY.PEEK[]<0.{read.MAX_FETCH_BYTES}>)", flags=False)
    html = []
    for uid in sorted(found, reverse=True):
        msg = email.message_from_bytes(found[uid][0], policy=email.policy.default)
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            try:
                html.append(part.get_content())
            except (LookupError, ValueError, UnicodeError):
                raise ValueError("A sent message could not be decoded for the reply guard.") from None
    return html


async def sent_html(account_id: str) -> list[str]:
    ep, username, password, check = await run_in_db(read.credentials, account_id)
    return await read._guarded(account_id, check, lambda: _sent_html(ep, username, password, account_id))
