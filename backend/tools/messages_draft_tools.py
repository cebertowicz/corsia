"""Texts through the email gate: ``messages_draft`` saves a draft document, ``messages_send`` sends it.

A send goes out only when ``tools.action_gate.check_action("message", …)`` approves, then through
``deliver_claimed`` like email: the draft is claimed once and released only when the failure proves
nothing was sent. The recipient is resolved when the draft is saved, so the person reads exactly where it goes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from connections.messages import account
from infra.db_async import run_in_db
import tools.messages_send as messages_send
import tools.messages_texts as texts
from tools import messages_db
from tools.action_delivery import deliver_claimed
from tools.action_drafts import DraftError
from tools.action_gate import Approved, check_action
from tools.base import Tool
from tools.reviewed_drafts import DraftStorageRefused, draft_target, open_draft, save_draft

log = logging.getLogger(__name__)


class _Refused(Exception):
    """A refusal whose full text goes back to the agent as written."""


def _resolve(chat_id: str, to: str) -> dict[str, Any]:
    """Where a draft goes: an existing chat's guid, or a new chat's handle."""
    db = messages_db.open_db()
    try:
        if chat_id:
            chat = messages_db.chat_by_id(db, chat_id)
            if chat is None:
                raise _Refused(f"Error: {texts.NO_CHAT}")
            return {"chat_guid": chat["guid"], "handle": None, "label": chat["label"], "service": chat["service"]}
        handle = messages_db.normalize_handle(to)
        if handle is None:
            raise _Refused(texts.BAD_TO)
        found = messages_db.chats_for_handle(db, handle)
        if len(found) > 1:
            raise _Refused(texts.AMBIGUOUS.format(ids=", ".join(chat["chat_id"] for chat in found)))
        if found:
            return {"chat_guid": found[0]["guid"], "handle": None, "label": found[0]["label"], "service": found[0]["service"]}
        return {"chat_guid": None, "handle": handle, "label": handle, "service": "iMessage"}
    finally:
        db.close()


def compose(label: str, service: str, body: str) -> str:
    return f"To: {' '.join(label.split())} · {service}\n\n{body}"


def parse_body(document: bytes) -> str | None:
    head, separator, body = document.decode("utf-8").partition("\n\n")
    return body if head.startswith("To: ") and separator and body.strip() else None


class MessagesDraftTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "messages_draft"

    @property
    def description(self) -> str:
        return texts.DRAFT + texts.AFTER_DRAFT

    @property
    def parameters(self) -> dict[str, Any]:
        text = {"type": "string"}
        return {
            "type": "object", "additionalProperties": False, "required": ["body"],
            "properties": {
                "chat_id": {**text, "description": "A chat_id from messages_chats."},
                "to": {**text, "description": "Full phone number with country code, or an email, for someone with no chat yet."},
                "body": {**text, "description": "The text to send."},
                "draft_id": {**text, "description": "An open draft to replace; omit for a new one."},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        if not messages_db.is_mac():
            return texts.NOT_A_MAC
        thread_id = kwargs.get("_thread_id")
        row = await account.usable_for_thread(thread_id)
        if row is None:
            return f"Error: {texts.NOT_CONNECTED}"
        chat_id, to, body = (str(kwargs.get(key) or "").strip() for key in ("chat_id", "to", "body"))
        if bool(chat_id) == bool(to):
            return texts.NEEDS_ONE
        if not body:
            return texts.NEEDS_BODY
        try:
            where = await asyncio.to_thread(_resolve, chat_id, to)
            target = {"account_id": str(row["id"]), "account": account.ADDRESS, **where}
            saved = await save_draft(
                thread_id=str(thread_id), kind="message", extension=".md", prefix="message-drafts",
                account_id=str(row["id"]), target=target, text=compose(where["label"], where["service"], str(kwargs["body"])),
                draft_id=kwargs.get("draft_id"), context=kwargs,
                description="Text draft; the person reviews it before anything is sent.",
            )
            path = str((await run_in_db(open_draft, str(thread_id), saved["draft_id"], "message"))["path"])
        except _Refused as exc:
            return str(exc)
        except messages_db.MessagesError as exc:
            return f"Error: {exc}"
        except (DraftStorageRefused, DraftError) as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("messages_draft failed")
            return "Error: Not saved: couldn't save the draft. Try again."
        return json.dumps({
            "draft_id": saved["draft_id"], "revision": saved["revision"], "path": path,
            "to": where["label"], "next": texts.AFTER_DRAFT,
        })


class MessagesSendTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "messages_send"

    @property
    def description(self) -> str:
        return texts.SEND

    @property
    def parameters(self) -> dict[str, Any]:
        text = {"type": "string"}
        return {
            "type": "object", "additionalProperties": False, "required": ["draft_id", "revision"],
            "properties": {
                "draft_id": {**text, "description": "The draft_id messages_draft returned."},
                "revision": {**text, "description": "The revision messages_draft returned."},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        draft_id, revision = kwargs.get("draft_id"), kwargs.get("revision")
        if not isinstance(draft_id, str) or not draft_id or not isinstance(revision, str) or not revision:
            return "Error: Pass the draft_id and revision messages_draft returned."
        if not messages_db.is_mac():
            return texts.NOT_A_MAC
        try:
            prepared = await self._prepare(kwargs.get("_thread_id"), draft_id, revision)
        except Exception:
            log.exception("messages_send could not check the draft")
            return texts.NOT_CHECKED
        if isinstance(prepared, str):
            return prepared
        target, body = prepared

        async def acquire() -> None:
            return None

        return await deliver_claimed(
            draft_id,
            acquire=acquire,
            provider_errors=(messages_send.SendFailed,),
            send=lambda _: messages_send.send(target.get("chat_guid"), target.get("handle"), body),
            result_id=lambda result: result,
            success=lambda _result, _external_id: texts.SENT.format(to=target["label"], service=target["service"]),
            sign_in_error=str,
            request_error=lambda exc: getattr(exc, "fixed", None) or str(exc),
            never_sent=lambda exc: isinstance(exc, messages_send.SendFailed) and exc.never_sent,
            still_sending=texts.STILL_SENDING,
            claim_error=texts.CLAIM_ERROR,
            unconfirmed=texts.UNCONFIRMED,
            label="messages_send",
        )

    async def _prepare(self, thread_id: Any, draft_id: str, revision: str) -> tuple[dict, str] | str:
        """The approved draft's target and text, or the error to return."""
        row = await run_in_db(open_draft, str(thread_id), draft_id, "message")
        if row is not None and row["state"] == "sending":
            return f"Error: {texts.STILL_SENDING}"
        verdict = await check_action("message", draft_id, revision)
        if not isinstance(verdict, Approved):
            return f"Error: {verdict.text}"
        target = draft_target(verdict.draft)
        if not target.get("account_id") or not (target.get("chat_guid") or target.get("handle")):
            return f"Error: {texts.NO_TARGET}"
        usable = await account.usable_for_thread(thread_id)
        if usable is None or str(usable["id"]) != target["account_id"]:
            return f"Error: Not sent: {texts.NOT_CONNECTED}"
        try:
            body = parse_body(verdict.data)
        except UnicodeDecodeError:
            body = None
        return f"Error: {texts.BAD_DOCUMENT}" if body is None else (target, body)
