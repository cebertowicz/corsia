"""What the draft view shows around an email draft, read from the draft's own account when it opens.

- ``GET /api/email-drafts/{artifact_id}/context`` — the conversation the draft replies into (newest first),
  which email it answers and that email's addresses, the files it attaches and its quiet notes

Only the signed-in person reads it, and only for a draft in a thread they own; anything else is a 404. The
account is the one the draft was saved with, never one the request names. Which recipients the send refuses rides
on the document's load and live frames (``email_document.read_header``); whether a recipient was added and whether
newer emails arrived the view works out from this data and that reading.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from connections.account_errors import AccountError, AccountUnavailable
from connections.mail.email_document import DraftRefused
from connections.mail_accounts import resolve_for_thread
from infra.auth import owns_thread, require_human_user
from infra.database import get_db
from infra.db_async import run_in_db
from infra.failure_reference import new_error_reference
from registry import artifact_record
from tools.mail_backends import MailBackend, backend_for
from tools.reviewed_drafts import draft_at, draft_target, working_root

router = APIRouter(prefix="/api/email-drafts", tags=["email-drafts"])

#: Why a conversation could not be read: the draft's account needs the person (signed out, removed, a password or
#: host setting to fix, or not an email account), or the read failed in a way trying again can fix.
ACCOUNT, UNREADABLE = "account", "unreadable"


def _draft_for(artifact_id: str, user_id: str) -> dict[str, Any] | None:
    """The email draft record of this artifact's file, when its thread is the person's own."""
    db = get_db()
    try:
        identity = artifact_record.identity_row_for(artifact_id, db=db)
        if identity is None or (identity["integration"], identity["type"]) != ("workspace", "document") or not identity["path"]:
            return None
        row = draft_at(Path(identity["path"]), "email", db=db)
        return row if row is not None and owns_thread(row["thread_id"], user_id, db=db) else None
    finally:
        db.close()


def _attachments(thread_id: str, pinned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The files the send attaches, from the draft record; one that was moved or deleted since is unavailable."""
    try:
        root = working_root(thread_id).resolve()
    except ValueError:
        root = None
    shown = []
    for item in pinned:
        path = (root / str(item.get("path", ""))).resolve() if root else None
        available = bool(path and path.is_relative_to(root) and path.is_file())
        kind = mimetypes.guess_type(str(item.get("name", "")))[0] or ""
        shown.append({"name": str(item.get("name", "")), "path": str(path) if path else "", "size": item.get("size") or 0,
                      "type": "image" if kind.startswith("image/") else "document", "unavailable": not available})
    return shown


async def _conversation(backend: MailBackend, account_id: str, target: dict[str, Any]) -> dict[str, Any]:
    """The conversation newest first, read with one provider call, and the addresses of the email being answered."""
    emails = sorted(await backend.conversation(account_id, target), key=lambda email: email.date, reverse=True)
    answered = next((list(email.addresses) for email in emails if email.message_id == target["reply_to"]), None)
    return {"status": "ready", "reason": None, "replied_to": target["reply_to"], "answered": answered,
            "conversation": [{"id": email.message_id, "sender": email.sender, "date": email.date.isoformat(),
                              "body": email.body} for email in emails]}


@router.get("/{artifact_id}/context")
async def draft_context(artifact_id: str, user: dict = Depends(require_human_user)) -> dict[str, Any]:
    draft = await run_in_db(_draft_for, artifact_id, user["id"])
    if draft is None:
        raise HTTPException(404, "Email draft not found")
    target = draft_target(draft)
    base = {"attachments": await asyncio.to_thread(_attachments, draft["thread_id"], target.get("attachments") or []),
            "notes": [str(note) for note in target.get("notes") or [] if isinstance(note, str)]}
    reply_to = target.get("reply_to")
    if not reply_to:
        # A new email: there is no conversation to read.
        return {**base, "status": "ready", "reason": None, "replied_to": None, "answered": None, "conversation": []}
    failed = {**base, "status": "error", "replied_to": str(reply_to), "answered": None, "conversation": []}
    if not target.get("account_id"):
        return {**failed, "reason": ACCOUNT}
    # A failed read shows a reference the person can quote, and the log line carries it (copy C10).
    reference = new_error_reference()
    try:
        account = await run_in_db(resolve_for_thread, target["account_id"], draft["thread_id"])
        backend = backend_for(account)
    except AccountUnavailable as exc:
        logger.warning("A draft's account isn't usable right now (reference {}): {}", reference, type(exc).__name__)
        return {**failed, "reason": UNREADABLE, "reference": reference}
    except AccountError:
        # Finding the account refuses only for an account problem (gone, not email, a setting to fix), except
        # what clears on its own above.
        return {**failed, "reason": ACCOUNT}
    try:
        return {**base, **await _conversation(backend, str(account["id"]), {**target, "reply_to": str(reply_to)})}
    except (AccountError, DraftRefused) as exc:
        # A read refuses for many reasons; only one the refusal itself marks as the person's to fix is ACCOUNT.
        if isinstance(exc, AccountError) and exc.person_fixes:
            logger.warning("A draft's conversation could not be read: the account needs the person ({})", type(exc).__name__)
            return {**failed, "reason": ACCOUNT}
        logger.warning("A draft's conversation could not be read (reference {}): {}: {}", reference, type(exc).__name__, exc)
    except Exception:  # noqa: BLE001 - any other read failure is the same named state, logged with its cause
        logger.exception("A draft's conversation could not be read (reference {})", reference)
    return {**failed, "reason": UNREADABLE, "reference": reference}
