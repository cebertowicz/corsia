"""Bounded IMAP metadata reads and lazy evidence reads for the reply guard."""
from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from connections.mail.email_document import DraftRefused, reply_addresses

from connections.mailhost import folders, imap, read
from infra.db_async import run_in_db
from tools.mail_guard import Message

_IDS = re.compile(r"<[^<>\s]+>")
_FLAGS = re.compile(rb"\bFLAGS\s*\(([^)]*)\)", re.I)
_INTERNAL_DATE = re.compile(rb'\bINTERNALDATE\s+"([^"]+)"', re.I)
_SENT_ID = re.compile(r"^sent:([0-9a-f]{8}):(\d{1,12}):(\d{1,12})$")
_FIELDS = "FROM DATE SUBJECT REPLY-TO MESSAGE-ID IN-REPLY-TO REFERENCES"


@dataclass
class _Mail:
    folder: str
    uid: int
    validity: int
    parsed: email.message.EmailMessage
    flags: bytes
    arrival: datetime
    own_id: str
    links: set[str]


# Selection is shared with the ordinary IMAP read path.
def _search(conn, ep, *terms: str) -> set[int]:
    status, data = conn.uid("SEARCH", *terms)
    if status != "OK" or not data or data[0] is None:
        raise imap.unreachable(ep)
    try:
        return {int(uid) for uid in data[0].split()}
    except (ValueError, AttributeError):
        raise imap.unreachable(ep) from None


def _batch(conn, ep, uids: set[int], query: str, *, flags: bool, missing_target: bool = False) -> dict[int, tuple[bytes, bytes, bytes]]:
    if not uids:
        return {}
    status, data = conn.uid("FETCH", ",".join(map(str, sorted(uids))), query)
    if status != "OK":
        raise imap.unreachable(ep)
    found: dict[int, tuple[bytes, bytes, bytes]] = {}
    for index, part in enumerate(data):
        if not isinstance(part, tuple) or not isinstance(part[0], bytes) or not isinstance(part[1], bytes):
            continue
        after = data[index + 1] if index + 1 < len(data) and isinstance(data[index + 1], bytes) else b""
        metadata = part[0] + b" " + after
        uid_match = imap.FETCHED.search(metadata)
        flag_match = _FLAGS.search(metadata)
        date_match = _INTERNAL_DATE.search(metadata)
        if uid_match and (flag_match or not flags) and (date_match or not flags):
            found[int(uid_match.group(1))] = (part[1], flag_match.group(1) if flag_match else b"",
                                               date_match.group(1) if date_match else b"")
    # Missing FETCH results are not a clean guard pass (including concurrent expunges).
    if set(found) != uids:
        if missing_target and not found and not list(imap.items(data)):
            raise read.MessageNotFound(read.NOT_FOUND)
        raise imap.unreachable(ep)
    return found


def _metadata(conn, ep, folder: str, validity: int, uids: set[int], *, missing_target: bool = False) -> list[_Mail]:
    query = f"(UID ENVELOPE FLAGS INTERNALDATE BODY.PEEK[HEADER.FIELDS ({_FIELDS})]<0.{read.MAX_FETCH_BYTES}>)"
    found = _batch(conn, ep, uids, query, flags=True, missing_target=missing_target)
    result = []
    for uid, (raw, flags, arrival) in found.items():
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        own = _IDS.findall(str(msg.get("Message-ID", "")))
        refs = _IDS.findall(" ".join(str(msg.get(name, "")) for name in ("In-Reply-To", "References")))
        own_id = own[0] if own else f"local:{folder}:{validity}:{uid}"
        try:
            arrived = datetime.strptime(arrival.decode('ascii'), '%d-%b-%Y %H:%M:%S %z').astimezone(timezone.utc)
        except (ValueError, UnicodeError):
            raise imap.unreachable(ep) from None
        result.append(_Mail(folder, uid, validity, msg, flags, arrived, own_id, set(refs) | {own_id}))
    return result


# Arrival times come from IMAP INTERNALDATE, never the message's Date header.
class _SentFolderMissing(ValueError):
    pass


def _sent_folder(conn, ep, username: str) -> str:
    folder = folders.resolve(conn, imap.folder_key(ep, username), folders.SENT, strict=True)
    if folder is None:
        raise _SentFolderMissing("Couldn't find the Sent folder.")
    return folder


def _thread_search(ids: set[str]) -> str:
    terms = [f'(HEADER {field} {imap.quoted(mid)})' for mid in sorted(ids)
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
                   subject=str(msg.get("Subject", "")), date=mail.arrival, body="",
                   reply_to=str(msg.get("Reply-To", "")), unread=b"\\seen" not in mail.flags.lower().split())


def _guard_data(ep, username: str, password: str, account_id: str, validity: int, uid: int, since: datetime | None):
    with imap.account_lock(account_id), imap.session(ep, username, password) as conn:
        if imap.select(conn, ep, imap.MAILBOX) != validity:
            raise read.MessageNotFound(read.NOT_FOUND)
        target = _metadata(conn, ep, imap.MAILBOX, validity, {uid}, missing_target=True)[0]
        if target.own_id.startswith("local:"):
            raise DraftRefused("The original message has no Message-ID, so a threaded reply isn't possible. Send a new email instead.")
        address = parseaddr(str(target.parsed.get("From", "")))[1]
        if not address:
            recipients = reply_addresses(str(target.parsed.get("Reply-To", "")), '')
            if recipients:
                raise DraftRefused(f"This message has no sender address, so later mail from them can't be checked. Write a new email to {recipients[0]} instead.")
            raise DraftRefused("The original message has no usable reply address. Send a new email to the correct address instead.")
        cutoff = since.replace(microsecond=0) if since is not None else target.arrival
        count = 0
        try:
            sent_folder = _sent_folder(conn, ep, username)
        except _SentFolderMissing:
            raise DraftRefused("The account has no Sent folder, so the reply cannot be checked. Create a Sent folder or use another account.") from None
        folder_names = (imap.MAILBOX, sent_folder)
        validities = {imap.MAILBOX: validity}
        found = {(imap.MAILBOX, uid): target}
        sender_keys: set[tuple[str, int]] = set()
        initial_ids = {link for link in target.links if not link.startswith("local:")}
        for folder in folder_names:
            current = imap.select(conn, ep, folder)
            if folder == imap.MAILBOX and current != validity:
                raise imap.unreachable(ep)
            validities[folder] = current
            thread_uids = _search(conn, ep, _thread_search(initial_ids))
            sender_uids = _search(conn, ep, "FROM", imap.quoted(address),
                                  "SINCE", (cutoff - timedelta(days=1)).strftime("%d-%b-%Y"))
            if since is None:
                count += len(_search(conn, ep, "FROM", imap.quoted(address), "SINCE",
                                     (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%d-%b-%Y")))
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
                if imap.select(conn, ep, folder) != validities[folder]:
                    raise imap.unreachable(ep)
                wanted = _search(conn, ep, _thread_search(next_ids))
                wanted -= {item_uid for item_folder, item_uid in found if item_folder == folder}
                for item in _metadata(conn, ep, folder, validities[folder], wanted):
                    found[(folder, item.uid)] = item
    ids = _thread_ids(list(found.values()))
    root = ids[(imap.MAILBOX, uid)]
    thread = [_message(account_id, item, ids[key]) for key, item in found.items() if ids[key] == root]
    sender = [_message(account_id, item, ids[key]) for key, item in found.items() if key in sender_keys
              and parseaddr(str(item.parsed.get("From", "")))[1].casefold() == address.casefold()]
    sender = [item for item in sender if item.date >= cutoff]
    if since is not None:
        target_id = _message(account_id, target, root).message_id
        thread = [item for item in thread if item.message_id != target_id and item.date >= cutoff]
        sender = [item for item in sender if item.message_id != target_id]
    return _message(account_id, target, root), thread, sender, count


async def guard_data(account_id: str, reply_message_id: str, since: datetime | None = None) -> tuple[Message, list[Message], list[Message], int]:
    """Return header-only messages. Call fetch_body for the target quote and screened evidence."""
    validity, uid = read.parse_message_id(account_id, reply_message_id)
    ep, username, password, check = await run_in_db(read.credentials, account_id)
    return await read.guarded(account_id, check, lambda: _guard_data(ep, username, password, account_id, validity, uid, since))


def _read_body(ep, username: str, password: str, account_id: str, folder_kind: str, validity: int, uid: int) -> str:
    with imap.account_lock(account_id):
        folder = imap.MAILBOX
        if folder_kind == "sent":
            with imap.session(ep, username, password) as conn:
                folder = _sent_folder(conn, ep, username)
        raw = imap.fetch(ep, username, password, validity, uid, read.MAX_FETCH_BYTES, folder)
    if raw is None:
        raise read.MessageNotFound(read.NOT_FOUND)
    return read.parse(raw, with_body=True)[1]


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
    return await read.guarded(account_id, check, lambda: _read_body(ep, username, password, account_id, folder_kind, validity, uid))


def _sent_html(ep, username: str, password: str, account_id: str) -> list[str]:
    with imap.account_lock(account_id), imap.session(ep, username, password) as conn:
        try:
            folder = _sent_folder(conn, ep, username)
        except _SentFolderMissing:
            # A successful folder listing with no Sent folder has no samples.
            return []
        imap.select(conn, ep, folder)
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
    return await read.guarded(account_id, check, lambda: _sent_html(ep, username, password, account_id))
