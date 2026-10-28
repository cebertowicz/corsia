"""Email through one of the person's Gmail or mail-host accounts: save a draft document, send it past the gate.

The draft is a workspace document the person reads and answers in chat. ``email_draft``
writes it, records its revision and binds it to the account it was written from;
``email_send`` sends it from that account, re-checking this thread's reach, only when
``tools.action_gate.check_action`` approves. It builds the email from the checked draft
and re-reads the sent-mail font at send time; for a reply it also re-reads the
conversation and quote. What differs by host is the account's backend (``tools.mail_backends``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from connections.account_errors import AccountError
from connections.google.client import GoogleSignInRejected
from connections.mailhost.errors import MailHostError, PASSWORD_REFUSED
from connections.mail_accounts import MAIL_ACCOUNT_PARAM, resolve_for_thread
from connections.mail import attachments, mail_compose
from connections.mail.email_document import (
    NEEDS_BODY,
    NEEDS_RECIPIENT,
    NEEDS_SUBJECT,
    DraftRefused,
    addresses,
    build_message,
    compose_document,
    parse_document,
    reply_subject,
    text_arg,
)
from infra.db_async import run_in_db
from tools.action_drafts import DraftError
from tools.reviewed_drafts import DraftStorageRefused, draft_target, open_draft, save_draft
from tools.action_gate import Approved, check_action
from tools.base import Tool
from tools.mail_backends import Gate, MailBackend, backend_for
from tools.mail_guard import evidence
from tools.mail_guard_runtime import guarded_reply, new_arrival

log = logging.getLogger(__name__)

NO_OPEN_DRAFT = "There is no open draft with that id in this conversation."
BAD_DOCUMENT = (
    "Not sent: the draft document isn't in the draft format. Save it again with email_draft, "
    "show the person, and wait for their reply."
)
STILL_SENDING = (
    "Not sent: an earlier send of this draft didn't finish, so it may already have gone out. "
    "Check the person's Sent mail; if it isn't there, save a new draft with email_draft and "
    "show the person."
)
AFTER_DRAFT = (
    "Show the person the draft (link its path), end your turn, and call email_send only when "
    "their reply says to send it; if they ask for changes, save again with email_draft and "
    "show them the new version."
)
FROM_MISMATCH = (
    "Not sent: the From line doesn't match the email account this draft was written from "
    "({account}). Save the draft again with email_draft and show the person."
)
OTHER_ACCOUNT = (
    "This draft was written from {address}. To use another account, save a new draft "
    "(omit draft_id) with that account_id and show the person."
)
NO_ACCOUNT = (
    "Not sent: this draft isn't tied to an email account. Save it again with email_draft and "
    "account_id, show the person, and wait for their reply."
)
GATE = Gate("email_send", STILL_SENDING, "Not sent: couldn't check the draft. Try again.")


def _stop(prefix: str, stops: list[str]) -> str:
    return f"{prefix}: " + "\n\n".join(stops)


async def _font(backend: MailBackend, account_id: str) -> tuple[str | None, str | None, str | None]:
    try:
        face, size = mail_compose.detected_font(await backend.sent_html(account_id))
        return face, size, None
    except AccountError as exc:
        if isinstance(exc, GoogleSignInRejected) or (isinstance(exc, MailHostError) and exc.cause == PASSWORD_REFUSED):
            raise
        log.warning("Couldn't read sent mail for font; using the provider default", exc_info=True)
    except (ValueError, UnicodeError, OSError):
        log.warning("Couldn't read sent mail for font; using the provider default", exc_info=True)
    return None, None, "Couldn’t read your usual font, so this email may go out in your mail’s default font."


UNGUARDED_REPLY = (
    "Not sent: this reply draft has no record of when its conversation was last read. Save it "
    "again with email_draft, then wait for the person's reply."
)


GUARD_GUIDANCE = (
    "If message text is withheld, use email_read or ask the person to look in their mail app. "
    "For new mail, save a new draft and wait for the person's reply. "
)


def _size(count: int) -> str:
    kb = round(count / 1024)
    if kb < 1024:
        return f"{max(1, kb) if count else 0} KB"
    return f"{count / (1024 * 1024):.1f} MB"


# Reviewed-draft storage is shared by provider families in tools.reviewed_drafts.

class EmailDraftTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "email_draft"

    @property
    def description(self) -> str:
        return (
            "Save an email as a draft document the person can read, from the account named "
            "by account_id. Gmail accounts append their signature. Nothing is sent. Returns "
            "draft_id and revision, which email_send needs. It is sent from the same account. "
            "Pass draft_id to replace an open draft; send the whole message again. Pass "
            "reply_to_message_id, a message id from the same account, to thread the email under "
            "that message. A replaced draft stays a reply to its earlier message unless you pass "
            "another one; for an email that is not a reply, start a new draft without draft_id. "
            "Pass workspace file paths in attachments to include them. "
            f"{AFTER_DRAFT}"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "draft_id": {"type": "string", "description": "An open draft to replace; omit for a new one."},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": "Plain recipient addresses; at least one, unless replying on a mail host."},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "Plain cc addresses."},
                "subject": {"type": "string", "description": "One line."},
                "body": {"type": "string", "description": "The message text, without a signature."},
                "reply_to_message_id": {"type": "string", "description": "The message id (not the thread id), from email_search or email_read on this account, to reply under. On a mail host the recipient defaults to the message's Reply-To, else its sender, and the subject gets Re: when it lacks it."},
                "attachments": {"type": "array", "items": {"type": "string"}, "description": "Workspace file paths to attach."},
                "account_id": MAIL_ACCOUNT_PARAM,
            },
            "required": ["subject", "body"],
        }

    async def execute(self, **kwargs: Any) -> str:
        thread_id = kwargs.get("_thread_id")
        try:
            account = await run_in_db(resolve_for_thread, kwargs.get("account_id"), thread_id)
            backend = backend_for(account)
        except AccountError as exc:
            return f"Error: {exc}"
        try:
            return await self._save(str(thread_id), account, backend, kwargs)
        except DraftRefused as exc:
            return f"Error: {exc}"
        except AccountError as exc:
            return f"Error: The draft was not saved. {exc}"
        except DraftError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("email_draft failed")
            return "Error: Not saved: couldn't check the account or save the draft. Try again."

    async def _save(self, thread_id: str, account: dict, backend: MailBackend, kwargs: dict[str, Any]) -> str:
        account_id, address = str(account["id"]), str(account["address"])
        to = addresses(kwargs.get("to"))
        if not to and not kwargs.get("reply_to_message_id") and not kwargs.get("draft_id"):
            raise DraftRefused(NEEDS_RECIPIENT)
        cc = addresses(kwargs.get("cc"))
        subject = text_arg(kwargs.get("subject"), NEEDS_SUBJECT, one_line=True)
        body = text_arg(kwargs.get("body"), NEEDS_BODY)
        draft_id = kwargs.get("draft_id")
        existing = await run_in_db(open_draft, thread_id, str(draft_id), "email") if draft_id is not None else None
        if existing is not None and existing["state"] != "open":
            raise DraftRefused(NO_OPEN_DRAFT)
        target, reply_label, signature = await backend.draft_metadata(account, existing, kwargs.get("reply_to_message_id"))
        notes: list[str] = []
        original = None
        if target.get("reply_to"):
            if not target.get("in_reply_to"):
                raise DraftRefused("Draft not saved: the original message has no Message-ID, so a threaded reply isn't possible. Send a new email instead.")
            target["guard_read_at"] = datetime.now(timezone.utc).isoformat()
            try:
                verdict, original = await guarded_reply(backend, account, str(target["reply_to"]), body, to)
            except DraftRefused as exc:
                raise DraftRefused(f"Not saved: {exc}") from None
            if verdict.stops:
                raise DraftRefused(_stop("Draft not saved", verdict.stops))
            to = to or verdict.recipients
            notes.extend(verdict.warnings + verdict.info + verdict.stale)
            notes.append("The message being replied to:\n" + evidence(original))
            notes.append(f"The earlier message from {original.sender} will be quoted below the reply.")
        if not to:
            raise DraftRefused(NEEDS_RECIPIENT)
        if target.get("prefix_reply_subject"):
            subject = reply_subject(subject)
        face, size, font_note = await _font(backend, account_id)
        notes.extend([font_note] if font_note else [])
        paths = kwargs.get("attachments") or []
        limit, applies_to_message = await backend.attachment_limit(account_id) if paths else (None, False)
        try:
            attached = await asyncio.to_thread(attachments.inspect, thread_id, paths, limit)
        except DraftRefused as exc:
            if str(exc).startswith("Not sent: "):
                raise DraftRefused("Draft not saved: " + str(exc).removeprefix("Not sent: ")) from None
            raise
        target = {**target, "account_id": account_id, "account": address, "attachments": attached}
        notes.extend(f"Attachment: {item['name']} ({_size(item['size'])})" for item in attached)
        text = compose_document(address, to, cc, subject, body, signature, reply_label, notes)
        if applies_to_message and limit is not None:
            parsed = parse_document(text)
            if parsed is None:
                raise DraftRefused("Couldn't prepare the draft. Check its contents and save it again.")
            sample = build_message(parsed, target)
            quote_body = await backend.fetch_body(account_id, original.message_id) if original else None
            mail_compose.add_html(sample, parsed["body"], face, size, quote_body)
            await asyncio.to_thread(attachments.add, thread_id, sample, attached)
            if len(sample.as_bytes()) > limit:
                raise DraftRefused(f"Draft not saved: {attachments.OVERSIZE}")
        try:
            saved = await save_draft(
                thread_id=thread_id, kind="email", extension=".md", prefix="email-drafts",
                account_id=account_id, target=target, text=text, draft_id=draft_id,
                context=kwargs, description="Email draft; the person reviews it before anything is sent.",
            )
        except DraftStorageRefused as exc:
            raise DraftRefused(str(exc)) from None
        # The reviewed draft is already durable; a host-side copy only adds a note.
        copy_note = await backend.keep_draft_copy(account, text, target, draft_target(existing))
        relative = str((await run_in_db(open_draft, thread_id, saved["draft_id"], "email"))["path"])
        result = {
            "draft_id": saved["draft_id"], "revision": saved["revision"], "path": relative,
            "from": address, "account_id": account_id, "next": AFTER_DRAFT,
        }
        if original:
            result["guard_evidence"] = verdict.evidence_ids
            result["guard_guidance"] = GUARD_GUIDANCE + f"Reply target: {original.message_id}; thread: {original.thread_id}."
        result.update({"font_note": font_note} if font_note else {})
        if copy_note is not None:
            result["note"] = copy_note
        return json.dumps(result)


# Reading and account binding are owned by tools.reviewed_drafts.

class EmailSendTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "email_send"

    @property
    def description(self) -> str:
        return (
            "Send a saved email draft from the email account it was written from. Call it only "
            "when the person's latest message says to send this draft. It goes out only if they "
            "wrote after the draft's last save and revision matches the one email_draft returned; "
            "otherwise nothing is sent and the error says what to do."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "draft_id": {"type": "string", "description": "The draft_id email_draft returned."},
                "revision": {"type": "string", "description": "The revision email_draft returned."},
            },
            "required": ["draft_id", "revision"],
        }

    async def execute(self, **kwargs: Any) -> str:
        draft_id, revision = kwargs.get("draft_id"), kwargs.get("revision")
        if not isinstance(draft_id, str) or not draft_id or not isinstance(revision, str) or not revision:
            return "Error: Pass the draft_id and revision email_draft returned."
        try:
            prepared = await self._prepare(kwargs.get("_thread_id"), draft_id, revision)
        except Exception:
            log.exception("email_send could not check the draft")
            return "Error: Not sent: couldn't check the draft. Try again."
        if isinstance(prepared, str):
            return prepared
        backend, account, message, target, saved_at = prepared
        try:
            original = None
            if saved_at is not None:
                original, newer = await new_arrival(backend, str(account["id"]), str(target["reply_to"]), saved_at)
                if newer:
                    return f"Error: Not sent: a newer message arrived:\n{newer}\n\nSave a new draft and wait for the person's reply."
            quote_body = await backend.fetch_body(str(account["id"]), original.message_id) if original else None
            body = str(message.get_content())
            face, size, _ = await _font(backend, str(account["id"]))
            mail_compose.add_html(message, body, face, size, quote_body)
            await asyncio.to_thread(attachments.add, str(kwargs.get("_thread_id")), message, target.get("attachments") or [])
        except (DraftRefused, AccountError) as exc:
            return f"Error: Not sent: {exc}"
        except Exception:
            log.exception("email_send guard or composition failed")
            return "Error: Not sent: couldn't check the account or prepare the message. Try again."
        return await backend.send_email(draft_id, account, message, target, GATE)

    async def _prepare(
        self, thread_id: Any, draft_id: str, revision: str,
    ) -> tuple[MailBackend, dict, EmailMessage, dict, datetime | None] | str:
        """The account's backend, the account, the message and the draft's target for an approved draft, or the error."""
        row = await run_in_db(open_draft, str(thread_id), draft_id, "email")
        if row is not None and row["state"] == "sending":
            return f"Error: {STILL_SENDING}"
        verdict = await check_action("email", draft_id, revision)
        if not isinstance(verdict, Approved):
            return f"Error: {verdict.text}"
        target = draft_target(verdict.draft)
        if not target.get("account_id"):
            return f"Error: {NO_ACCOUNT}"
        try:
            account = await run_in_db(resolve_for_thread, target["account_id"], thread_id)
            backend = backend_for(account)
        except AccountError as exc:
            return f"Error: Not sent: {exc}"
        try:
            parsed = parse_document(verdict.data.decode("utf-8"))
            if parsed is None:
                return f"Error: {BAD_DOCUMENT}"
            # Display only: the sender is always the bound account, never this line.
            if parsed["from"] is not None and parsed["from"].lower() != str(account["address"]).lower():
                return f"Error: {FROM_MISMATCH.format(account=account['address'])}"
            message = build_message(parsed, target)
        except (UnicodeDecodeError, ValueError):
            return f"Error: {BAD_DOCUMENT}"
        if not target.get("reply_to"):
            return backend, account, message, target, None
        if not target.get("guard_read_at"):
            return f"Error: {UNGUARDED_REPLY}"
        saved_at = datetime.fromisoformat(target["guard_read_at"])
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        return backend, account, message, target, saved_at
