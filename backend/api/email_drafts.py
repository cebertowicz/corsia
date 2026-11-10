"""What the draft view shows around an email draft, read from the draft's own account when it opens.

- ``GET /api/email-drafts/{artifact_id}/context`` — the conversation the draft replies into (newest first),
  which email it answers and that email's addresses, the files it attaches and its quiet notes

Only the signed-in person reads it, and only for a draft in a thread they own; anything else is a 404. The
account is the one the draft was saved with, never one the request names. Who the email goes to and whether
newer emails arrived are not worked out here: the view derives them from this data and the draft's own text.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from connections.account_errors import AccountError
from connections.mail.email_document import DraftRefused
from connections.mail_accounts import resolve_for_thread
from infra.auth import owns_thread, require_human_user
from infra.database import get_db
from infra.db_async import run_in_db
from core.workspace_layout import resolve_workspace_root
from infra import config
from registry import artifact_record
from tools.mail_backends import MailBackend, backend_for
from tools.reviewed_drafts import draft_target, working_root

router = APIRouter(prefix="/api/email-drafts", tags=["email-drafts"])

#: Why a conversation could not be read: the draft's account can't be used here (signed out, removed or not an
#: email account), or the account answered but the read failed.
ACCOUNT, UNREADABLE = "account", "unreadable"


def _draft_for(artifact_id: str, user_id: str) -> dict[str, Any] | None:
    """The email draft stored at this artifact's file, when its thread is the person's own."""
    db = get_db()
    try:
        identity = artifact_record.identity_row_for(artifact_id, db=db)
        if identity is None or (identity["integration"], identity["type"]) != ("workspace", "document") or not identity["path"]:
            return None
        # The draft record keeps its path relative to the workspace (tools/reviewed_drafts.py).
        relative = os.path.relpath(Path(identity["path"]).resolve(), resolve_workspace_root(config.WORKSPACE).resolve())
        row = db.execute(
            "SELECT id,thread_id,path,target_json FROM action_drafts WHERE kind='email' AND path=? "
            "ORDER BY created_at DESC LIMIT 1", (relative,),
        ).fetchone()
        if row is None or not owns_thread(row["thread_id"], user_id, db=db):
            return None
        return dict(row)
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


def _addresses(*headers: str) -> list[str]:
    return [address.lower() for _, address in getaddresses([h for h in headers if h]) if address]


async def _conversation(backend: MailBackend, account_id: str, reply_to: str) -> dict[str, Any]:
    """The thread newest first with each email's text; an email whose text can't be read is left out and counted."""
    target, thread, _, _ = await backend.guard_data(account_id, reply_to)
    thread = sorted(thread, key=lambda message: message.date, reverse=True)

    async def text(message) -> tuple[str, list[str] | None]:
        if message.message_id != target.message_id:
            return await backend.fetch_body(account_id, message.message_id), None
        # The answered email is read whole: its body, and the addresses it went to besides its sender.
        read = await backend.read.fetch(account_id, message.message_id)
        return read.body, _addresses(read.headers.get("from", ""), read.headers.get("to", ""), read.headers.get("cc", ""))

    results = await asyncio.gather(*(text(message) for message in thread), return_exceptions=True)
    conversation, answered = [], None
    for message, result in zip(thread, results):
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            logger.warning("An email in a draft's conversation could not be read: {}", type(result).__name__)
            continue
        body, addresses = result
        answered = addresses if addresses is not None else answered
        conversation.append({"id": message.message_id, "sender": message.sender, "date": message.date.isoformat(), "body": body})
    unreadable = len(thread) - len(conversation)
    return {"status": "partial" if unreadable else "ready", "reason": None, "replied_to": target.message_id,
            "answered": answered, "conversation": conversation, "unreadable": unreadable}


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
        return {**base, "status": "ready", "reason": None, "replied_to": None, "answered": None, "conversation": [], "unreadable": 0}
    failed = {**base, "status": "error", "replied_to": str(reply_to), "answered": None, "conversation": [], "unreadable": 0}
    if not target.get("account_id"):
        return {**failed, "reason": ACCOUNT}
    try:
        account = await run_in_db(resolve_for_thread, target["account_id"], draft["thread_id"])
        backend = backend_for(account)
    except AccountError:
        return {**failed, "reason": ACCOUNT}
    try:
        return {**base, **await _conversation(backend, str(account["id"]), str(reply_to))}
    except (AccountError, DraftRefused) as exc:
        logger.warning("A draft's conversation could not be read: {}", type(exc).__name__)
    except Exception:  # noqa: BLE001 - any other read failure is the same named state, logged with its cause
        logger.exception("A draft's conversation could not be read")
    return {**failed, "reason": UNREADABLE}
