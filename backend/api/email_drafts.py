"""What the draft view shows around an email draft, read from the draft's own account when it opens.

- ``GET /api/email-drafts/{artifact_id}/context`` — the conversation the draft replies into (newest first),
  which email it answers and that email's addresses, the files it attaches and its quiet notes
- ``POST /api/email-drafts/{artifact_id}/send`` — Send now: send the exact text on the person's screen
- ``POST /api/email-drafts/{artifact_id}/discard`` — Don't send: mark the draft not to send

Only the signed-in person reads or acts, and only on a draft in a thread they own; anything else is a 404. The
account is the one the draft was saved with, never one the request names. Which recipients the send refuses rides
on the document's load and live frames (``email_document.read_header``); whether a recipient was added and whether
newer emails arrived the view works out from this data and that reading.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from connections.account_errors import AccountError
from connections.mail.email_document import DraftRefused, parse_document
from connections.mail_accounts import resolve_for_thread
from infra.auth import owns_thread, require_human_user
from infra.database import get_db
from infra.db_async import run_in_db
from infra.failure_reference import new_error_reference
from registry import artifact_record
from tools.mail_backends import MailBackend, backend_for
from doceditor.document_scope import revision as document_revision
from tools.action_drafts import DraftError, discard_draft, record_draft
from tools.action_gate import _read_bytes
from tools.email_send_approved import IN_FLIGHT, NEWER, SENT, UNCONFIRMED, SendResult, send_approved
from tools.reviewed_drafts import draft_lock, draft_record_at, draft_target, working_root

router = APIRouter(prefix="/api/email-drafts", tags=["email-drafts"])

#: Why a conversation could not be read: the draft's account needs the person (signed out, removed, a password or
#: host setting to fix, or not an email account), or the read failed in a way trying again can fix.
ACCOUNT, UNREADABLE = "account", "unreadable"


def _draft_for(artifact_id: str, user_id: str) -> dict[str, Any] | None:
    """The email draft record of this artifact's file, in any state, when its thread is the person's own."""
    db = get_db()
    try:
        identity = artifact_record.identity_row_for(artifact_id, db=db)
        if identity is None or (identity["integration"], identity["type"]) != ("workspace", "document") or not identity["path"]:
            return None
        row = draft_record_at(Path(identity["path"]), "email", db=db)
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


class RemoveAttachment(BaseModel):
    path: str


@router.delete("/{artifact_id}/attachments/{index}")
async def remove_attachment(
    artifact_id: str, index: int, body: RemoveAttachment, user: dict = Depends(require_human_user),
) -> dict[str, Any]:
    """Remove the displayed attachment from the person's open draft under its existing lock."""
    draft = await run_in_db(_draft_for, artifact_id, user["id"])
    if draft is None:
        raise HTTPException(404, "Email draft not found")
    async with draft_lock(str(draft["id"])):
        draft = await run_in_db(_draft_for, artifact_id, user["id"])
        if draft is None:
            raise HTTPException(404, "Email draft not found")
        if draft["state"] != "open":
            raise HTTPException(409, {"state": draft["state"]})
        target = draft_target(draft)
        pinned = target.get("attachments") or []
        shown = await asyncio.to_thread(_attachments, draft["thread_id"], pinned)
        if index < 0 or index >= len(shown) or shown[index]["path"] != body.path:
            raise HTTPException(409, {"state": "stale"})
        data = await asyncio.to_thread(_read_bytes, str(draft["path"]))
        if data is None:
            raise HTTPException(409, {"state": "stale"})
        target["attachments"] = pinned[:index] + pinned[index + 1:]
        try:
            await run_in_db(record_draft, str(draft["id"]), data, target)
        except DraftError:
            raise HTTPException(409, {"state": "stale"}) from None
        saved = await run_in_db(_draft_for, artifact_id, user["id"])
        if saved is None:
            raise HTTPException(404, "Email draft not found")
        return {"attachments": await asyncio.to_thread(_attachments, draft["thread_id"], target["attachments"]),
                "target_version": target_version(saved)}


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
    # ``read_at`` is when this read began, and only a read that returned the conversation carries it: a Send now that
    # carries it doesn't count the emails this view showed as newer, and a failed read showed none.
    read_at = datetime.now(timezone.utc).isoformat()
    # The draft's own chat opens its attachments, whichever chat is on screen.
    base = {"thread_id": draft["thread_id"], "state": draft["state"], "read_at": None,
            "target_version": target_version(draft),
            "attachments": await asyncio.to_thread(_attachments, draft["thread_id"], target.get("attachments") or []),
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
    except AccountError as exc:
        # The refusal itself says whether only the person can fix it, and its fixed text names the cause; anything
        # else may clear on a retry.
        if exc.person_fixes:
            return {**failed, "reason": ACCOUNT, "detail": str(exc)}
        logger.warning("A draft's account isn't usable right now (reference {}): {}", reference, type(exc).__name__)
        return {**failed, "reason": UNREADABLE, "reference": reference}
    try:
        return {**base, **await _conversation(backend, str(account["id"]), {**target, "reply_to": str(reply_to)}),
                "read_at": read_at}
    except (AccountError, DraftRefused) as exc:
        # The same rule as finding the account: only a refusal marked as the person's to fix is ACCOUNT.
        if isinstance(exc, AccountError) and exc.person_fixes:
            logger.warning("A draft's conversation could not be read: the account needs the person ({})", type(exc).__name__)
            return {**failed, "reason": ACCOUNT, "detail": str(exc)}
        logger.warning("A draft's conversation could not be read (reference {}): {}: {}", reference, type(exc).__name__, exc)
    except Exception:  # noqa: BLE001 - any other read failure is the same named state, logged with its cause
        logger.exception("A draft's conversation could not be read (reference {})", reference)
    return {**failed, "reason": UNREADABLE, "reference": reference}


#: What the person reads when the click didn't send. The text on their screen isn't what the draft holds now.
CHANGED = "The draft changed; review it again."
NEWER_LINE = "A newer email arrived in this conversation; review it again."


def target_version(draft: dict[str, Any]) -> str:
    """Which stored target (attachments, account, reply, notes) the view showed: an agent's save that changes only the
    target leaves the text alone, so the text check can't see it."""
    return hashlib.sha256(str(draft.get("target_json") or "{}").encode("utf-8")).hexdigest()


class SendNow(BaseModel):
    """The draft's text as the person's screen shows it, the target its view read, and when it read the conversation.

    A send without ``target_version`` is refused: what goes out besides the text was never shown.
    """

    text: str
    target_version: str | None = None
    read_at: str | None = None


def _read_at(value: str | None) -> datetime | None:
    """The view's read time, never later than now: a clock the client sets can't move the newer-email cut-off ahead."""
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if parsed is None:
        return None
    parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return min(parsed, datetime.now(timezone.utc))


def _first_recipient(data: bytes) -> str:
    parsed = parse_document(data.decode("utf-8", errors="replace"))
    return parsed["to"][0] if parsed and parsed["to"] else "the recipient"


_AGENT = {
    SENT: "The person sent email draft {id} with Send now (revision {revision}, {at}). It has gone out; there is "
          "nothing more to send.{review}{extra}",
    "failed": "The person pressed Send now on email draft {id} from {account}, but it wasn't sent: {reason}. The "
              "draft is still open, and they can press Send now again once it's fixed.",
    UNCONFIRMED: "The person pressed Send now on email draft {id} from {account}, but the mail service didn't "
                 "confirm it, so it may have gone out. Don't resend it; ask them to check their Sent mail. If it "
                 "isn't there, save a new draft when they ask for one.",
    "discarded": "The person chose Don't send on email draft {id}. It won't be sent. Save a new draft only if they "
                 "ask for another version.{review}",
}
#: How the notice ends, by what became of the draft's review request (``_answer_review``).
_REVIEW = {
    True: " Your review request on it is answered.",
    False: " Its review request couldn't be marked answered; settle it yourself.",
    None: "",
}
#: Don't send answers the review as changes requested, with a note that says what that means here.
DECLINED_NOTE = "Not sent: the person chose Don't send. Save a new draft only if they ask for one."
_PERSON = {
    SENT: "You sent the email to {to}.{extra}",
    "failed": "The email to {to} wasn't sent.",
    UNCONFIRMED: "The email to {to} may have gone out. Check your Sent mail.",
    "discarded": "You chose not to send the email to {to}.",
}


def _answer_review(artifact_id: str, outcome: str, woken: list[str]) -> bool | None:
    """Send now or Don't send settles the draft's review request in the same request (E7), so no stale item stays in
    the person's list. True: answered. None: no request was open. False: the answer failed (logged); the agent's
    notice says so. Every open request on the draft is answered: a draft's review is the question whether to send it."""
    from doceditor.review_bar import AnswerRefused
    from registry.attention import clear_attention

    verdict, note = ("approved", None) if outcome == SENT else ("changes_requested", DECLINED_NOTE)
    try:
        clear_attention(artifact_id, outcome="answer", verdict=verdict, note=note, drives=woken)
    except AnswerRefused as exc:
        if exc.code == "ask_stale":
            return None
        logger.warning("draft-send artifact={}: the review answer was refused ({})", artifact_id, exc.code)
        return False
    except Exception:  # noqa: BLE001 - the email's outcome is already stored; only the review answer failed
        logger.exception("draft-send artifact={}: answering the review failed", artifact_id)
        return False
    return True


async def _settle_review(artifact_id: str, outcome: str) -> bool | None:
    woken: list[str] = []
    answered = await run_in_db(_answer_review, artifact_id, outcome, woken)
    if woken:
        from plans.public import drive_threads

        await drive_threads(woken)
    return answered


async def _tell_agent(draft: dict[str, Any], outcome: str, *, revision: str, to: str, account: str | None,
                      reason: str | None = None, refused: tuple[str, ...] = (), note: str | None = None,
                      review: bool | None = None) -> None:
    """The draft's own agent learns each outcome, and the row it reads is the send's record: draft, revision,
    outcome and time, never the body. Best-effort: the draft's state is already stored."""
    from events.delivery import deliver_to_thread

    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    logger.info("draft-send draft={} revision={} outcome={} at={}", draft["id"], revision, outcome, at)
    missed = f" These addresses did not get it: {', '.join(refused)}." if refused else ""
    summary = _AGENT[outcome].format(id=draft["id"], revision=revision, at=at, account=account or "its account",
                                     reason=reason or "the mail service refused it", review=_REVIEW[review],
                                     extra=missed + (f" {note}" if note else ""))
    try:
        delivered = await deliver_to_thread(
            str(draft["thread_id"]), f"email_draft.{outcome}", summary,
            {"draft_id": draft["id"], "revision": revision, "outcome": outcome, "at": at,
             **({"not_delivered_to": list(refused)} if refused else {})},
            delivery_id=f"draft-send:{draft['id']}:{outcome}:{revision}", chip=_PERSON[outcome].format(to=to, extra=f" {', '.join(refused)} didn't get it." if refused else ""),
        )
        if not delivered:
            logger.warning("draft-send draft={}: telling the agent {} failed", draft["id"], outcome)
    except Exception:  # noqa: BLE001 - the send already happened or provably didn't; only the notice failed
        logger.exception("draft-send draft={}: telling the agent {} failed", draft["id"], outcome)


def _settled(draft: dict[str, Any]) -> dict[str, Any] | None:
    """The answer for a draft that is no longer open: a second click, or another tab, finds it settled."""
    state = draft["state"]
    if state == "open":
        return None
    return {"state": state}


async def _now_settled(artifact_id: str, user_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    """Where a draft stands after another request moved it first (a second tab, a double click)."""
    current = await run_in_db(_draft_for, artifact_id, user_id) or draft
    return _settled(current) or {"state": IN_FLIGHT}


@router.post("/{artifact_id}/send")
async def send_now(artifact_id: str, body: SendNow, user: dict = Depends(require_human_user)) -> dict[str, Any]:
    """Send the draft as the person sees it, with no model turn: the click is their approval of these exact bytes."""
    found = await run_in_db(_draft_for, artifact_id, user["id"])
    if found is None:
        raise HTTPException(404, "Email draft not found")
    # An agent's save of this draft waits for the send to settle, and a save already under way finishes first, so the
    # bytes compared here are the bytes that go out and the bytes the record names.
    async with draft_lock(str(found["id"])):
        draft = await run_in_db(_draft_for, artifact_id, user["id"]) or found
        if (settled := _settled(draft)) is not None:
            return settled
        live = await asyncio.to_thread(_read_bytes, str(draft["path"]))
        if live is None or live != body.text.encode("utf-8") or body.target_version != target_version(draft):
            raise HTTPException(409, {"state": "changed", "message": CHANGED})
        revision = document_revision(live)
        if revision != draft["recorded_revision"]:
            # The person edited the draft: record what they approved, so the record names exactly what is sent.
            try:
                await run_in_db(record_draft, str(draft["id"]), live)
            except DraftError:
                return await _now_settled(artifact_id, user["id"], draft)
        result: SendResult = await send_approved(draft, live, read_at=_read_at(body.read_at))
    if result.state == NEWER:
        raise HTTPException(409, {"state": NEWER, "message": NEWER_LINE})
    if result.state == IN_FLIGHT:
        # Another click or tab claimed it first; answer where that one stands now.
        return await _now_settled(artifact_id, user["id"], draft)
    review = await _settle_review(artifact_id, SENT) if result.state == SENT else None
    await _tell_agent(draft, result.state, revision=revision, to=_first_recipient(live), account=result.account,
                      reason=result.reason, refused=result.not_delivered_to, note=result.note, review=review)
    if result.state == "failed":
        return {"state": result.state, "reason": result.reason}
    # The person must not read 'Sent' as 'everyone got it' when the mail host refused some addresses.
    return {"state": result.state, **({"not_delivered_to": list(result.not_delivered_to)} if result.not_delivered_to else {})}


@router.post("/{artifact_id}/discard")
async def dont_send(artifact_id: str, user: dict = Depends(require_human_user)) -> dict[str, Any]:
    """Don't send: the draft is marked not to send, once, and its agent is told."""
    draft = await run_in_db(_draft_for, artifact_id, user["id"])
    if draft is None:
        raise HTTPException(404, "Email draft not found")
    if (settled := _settled(draft)) is not None:
        return settled
    try:
        await run_in_db(discard_draft, str(draft["id"]))
    except DraftError:
        return await _now_settled(artifact_id, user["id"], draft)
    live = await asyncio.to_thread(_read_bytes, str(draft["path"])) or b""
    review = await _settle_review(artifact_id, "discarded")
    await _tell_agent(draft, "discarded", revision=document_revision(live), to=_first_recipient(live), account=None,
                      review=review)
    return {"state": "discarded"}
