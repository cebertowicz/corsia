"""Email through the person's Google sign-in: save a draft document, send it past the gate.

The draft is a workspace document the person reads and answers in chat. ``email_draft``
writes it and records its revision; ``email_send`` sends it only when
``tools.action_gate.check_action`` approves, and builds the message from the bytes the
gate checked, never from a second read. Every Gmail URL is a fixed template with ids
URL-encoded, so no model-built URL reaches the HTTP client.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from base64 import urlsafe_b64encode
from pathlib import Path
from typing import Any
from urllib.parse import quote

from connections.google import client as google_client
from connections.google import oauth
from connections.google.client import GoogleError, GoogleRequestFailed
from connections.google.email_document import (
    NEEDS_BODY,
    NEEDS_RECIPIENT,
    NEEDS_SUBJECT,
    DraftRefused,
    addresses,
    build_message,
    compose_document,
    parse_document,
    signature_text,
    text_arg,
)
from infra import config
from infra.db_async import run_in_db
from tools.action_drafts import (
    DraftError,
    claim_for_send,
    create_draft,
    mark_sent,
    record_draft,
    release_claim,
)
from tools.action_gate import Approved, check_action
from tools.base import Tool
from tools.google_account import _designated

log = logging.getLogger(__name__)

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SEND_AS_URL = f"{GMAIL}/settings/sendAs"
SEND_URL = f"{GMAIL}/messages/send"
DRAFT_DIR = "email-drafts"

NO_MESSAGE = "That message can't be found."
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
UNCONFIRMED = (
    "Google didn't confirm the send, so the email may have gone out. Don't send this draft "
    "again: check the person's Sent mail first."
)


def _message_url(message_id: str) -> str:
    return f"{GMAIL}/messages/{quote(message_id, safe='')}"


async def _reply_target(message_id: str) -> dict:
    """Threading facts from the message's headers only; no body is requested."""
    try:
        found = await google_client.request_json(
            "GET", _message_url(message_id),
            params={"format": "metadata", "metadataHeaders": ["Message-ID", "References"]},
        )
    except GoogleRequestFailed as exc:
        if exc.status == 404:
            raise DraftRefused(NO_MESSAGE) from None
        raise
    if not isinstance(found, dict):
        raise DraftRefused(NO_MESSAGE)
    headers = {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in (found.get("payload") or {}).get("headers", []) if isinstance(item, dict)
    }
    message_header = headers.get("message-id", "").strip()
    references = " ".join(part for part in (headers.get("references", "").strip(), message_header) if part)
    return {
        "reply_to": message_id,
        "thread_id": found.get("threadId"),
        "in_reply_to": message_header or None,
        "references": references or None,
    }


async def _signature() -> str:
    found = await google_client.get_json(SEND_AS_URL)
    aliases = found.get("sendAs") if isinstance(found, dict) else None
    if not isinstance(aliases, list):
        return ""
    chosen = next(
        (alias for alias in aliases if isinstance(alias, dict) and alias.get("isPrimary")),
        next((alias for alias in aliases if isinstance(alias, dict)), None),
    )
    html = chosen.get("signature") if chosen else None
    return signature_text(html) if isinstance(html, str) and html.strip() else ""


def _workspace_root() -> Path:
    return config.WORKSPACE.resolve()


def _new_draft_path(thread_id: str) -> Path:
    """A new, unused document path in the thread's working tray."""
    from core.workspace_layout import (
        resolve_project_root,
        resolve_thread_working_root,
        resolve_workspace_root,
    )
    from infra.database import get_db

    workspace = resolve_workspace_root(config.WORKSPACE)
    db = get_db()
    try:
        row = db.execute("SELECT project_id FROM threads WHERE id = ?", (thread_id,)).fetchone()
        project_id = row["project_id"] if row else None
        scope = resolve_project_root(workspace, project_id, db=db) if project_id else workspace
    finally:
        db.close()
    if scope is None:
        raise DraftRefused("The project's folder is unavailable, so the draft was not saved.")
    return resolve_thread_working_root(scope, thread_id) / DRAFT_DIR / f"email-{uuid.uuid4().hex[:12]}.md"


def _relative(path: Path) -> str:
    relative = os.path.relpath(path.resolve(), _workspace_root())
    if relative.startswith(".."):
        raise DraftRefused("The draft's folder is outside the workspace, so it was not saved.")
    return relative


def _open_draft(thread_id: str, draft_id: str) -> dict | None:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute(
            "SELECT id, path, state, target_json FROM action_drafts"
            " WHERE id=? AND thread_id=? AND kind='email'",
            (draft_id, thread_id),
        ).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


class EmailDraftTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "email_draft"

    @property
    def description(self) -> str:
        return (
            "Save an email as a draft document the person can read, with their Gmail signature "
            "appended. Nothing is sent. Pass draft_id to replace an open draft; every field is "
            "required again, so send the whole message. Pass reply_to_message_id to thread the "
            "email under a message. Show the person the draft, end your turn, and call email_send "
            "only after their reply."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "draft_id": {"type": "string", "description": "An open draft to replace; omit for a new one."},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": "Plain recipient addresses; at least one."},
                "cc": {"type": "array", "items": {"type": "string"}, "description": "Plain cc addresses."},
                "subject": {"type": "string", "description": "One line."},
                "body": {"type": "string", "description": "The message text, without a signature."},
                "reply_to_message_id": {"type": "string", "description": "A Gmail message id to reply under."},
            },
            "required": ["to", "subject", "body"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        thread_id = kwargs["_thread_id"]
        try:
            return await self._save(thread_id, kwargs)
        except DraftRefused as exc:
            return f"Error: {exc}"
        except GoogleError as exc:
            return f"Error: The draft was not saved. {exc}"
        except DraftError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("email_draft failed")
            return "Error: Couldn't save the draft."

    async def _save(self, thread_id: str, kwargs: dict[str, Any]) -> str:
        from tools.filesystem import WriteFileTool

        to = addresses(kwargs.get("to"))
        if not to:
            raise DraftRefused(NEEDS_RECIPIENT)
        cc = addresses(kwargs.get("cc"))
        subject = text_arg(kwargs.get("subject"), NEEDS_SUBJECT, one_line=True)
        body = text_arg(kwargs.get("body"), NEEDS_BODY)
        draft_id = kwargs.get("draft_id")
        existing = None
        if draft_id is not None:
            existing = await run_in_db(_open_draft, thread_id, str(draft_id))
            if existing is None or existing["state"] != "open":
                raise DraftRefused(NO_OPEN_DRAFT)
        reply_id = kwargs.get("reply_to_message_id")
        target = await _reply_target(str(reply_id)) if reply_id else None
        signature = await _signature()
        account = await run_in_db(oauth.account_status)
        text = compose_document(to, cc, subject, body, signature)

        if existing is not None:
            path = _workspace_root() / existing["path"]
        else:
            path = await run_in_db(_new_draft_path, thread_id)
        relative = _relative(path)
        context = {key: value for key, value in kwargs.items() if key.startswith("_")}
        written = await WriteFileTool(workspace=config.WORKSPACE).execute(
            path=str(path), content=text,
            description="Email draft; the person reviews it before anything is sent.",
            **context,
        )
        if isinstance(written, str) and written.startswith("Error"):
            return written
        data = await run_in_db(_read_document, relative)
        if data is None:
            raise DraftRefused("The draft was written but couldn't be read back, so it is not recorded.")
        if existing is not None:
            saved = await run_in_db(record_draft, existing["id"], data, target)
        else:
            saved = await run_in_db(create_draft, thread_id, "email", relative, data, target or {})
        return json.dumps({
            "draft_id": saved["draft_id"], "revision": saved["revision"], "path": relative,
            "from": account.get("email"),
            "next": "Show the person this draft and end your turn; call email_send only after they reply.",
        })


def _read_document(relative: str) -> bytes | None:
    from tools.action_gate import _read_bytes

    return _read_bytes(relative)


class EmailSendTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "email_send"

    @property
    def description(self) -> str:
        return (
            "Send a saved email draft. It goes out only when the person wrote in this turn after the "
            "draft's last save and revision is the one email_draft returned; otherwise nothing is sent "
            "and the error says what to do."
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
        if refusal := await _designated(kwargs):
            return refusal
        draft_id, revision = kwargs.get("draft_id"), kwargs.get("revision")
        if not isinstance(draft_id, str) or not draft_id or not isinstance(revision, str) or not revision:
            return "Error: Pass the draft_id and revision email_draft returned."
        try:
            prepared = await self._prepare(kwargs["_thread_id"], draft_id, revision)
        except Exception:
            log.exception("email_send could not check the draft")
            return "Error: Not sent: couldn't check the draft. Try again."
        if isinstance(prepared, str):
            return prepared
        return await self._deliver(draft_id, prepared)

    async def _prepare(self, thread_id: str, draft_id: str, revision: str) -> dict | str:
        """The Gmail request body for an approved draft, or the tool's error text. Sends nothing."""
        row = await run_in_db(_open_draft, thread_id, draft_id)
        if row is not None and row["state"] == "sending":
            return f"Error: {STILL_SENDING}"
        verdict = await check_action("email", draft_id, revision)
        if not isinstance(verdict, Approved):
            return f"Error: {verdict.text}"
        try:
            parsed = parse_document(verdict.data.decode("utf-8"))
            if parsed is None:
                return f"Error: {BAD_DOCUMENT}"
            target = json.loads(verdict.draft.get("target_json") or "{}")
            target = target if isinstance(target, dict) else {}
            raw = urlsafe_b64encode(build_message(parsed, target).as_bytes()).decode("ascii")
        except (UnicodeDecodeError, ValueError):
            return f"Error: {BAD_DOCUMENT}"
        body: dict[str, Any] = {"raw": raw}
        if target.get("thread_id"):
            body["threadId"] = target["thread_id"]
        return body

    async def _deliver(self, draft_id: str, body: dict) -> str:
        """Claim the draft, send it once, and settle it by what Google proved."""
        try:
            await run_in_db(claim_for_send, draft_id)
        except DraftError:
            return f"Error: {STILL_SENDING}"
        except Exception:
            log.exception("email_send could not claim the draft")
            return "Error: Not sent: couldn't check the draft. Try again."
        try:
            bearer = await google_client.access_token()
        except GoogleError as exc:
            await self._release(draft_id)
            return f"Error: Not sent: {exc}"
        except Exception:
            log.exception("email_send could not get a Google sign-in")
            await self._release(draft_id)
            return "Error: Not sent: couldn't reach Google. Try again later."
        # From here the request may have reached Gmail: only a proof that it did not
        # (never connected, or a 4xx) returns the draft to open.
        try:
            sent = await google_client.request_json("POST", SEND_URL, body=body, token=bearer)
        except google_client.GoogleSignInRejected as exc:
            await self._release(draft_id)
            return f"Error: Not sent: {exc}"
        except GoogleRequestFailed as exc:
            if exc.never_sent:
                await self._release(draft_id)
                return f"Error: Not sent: {exc}"
            return f"Error: {UNCONFIRMED}"
        except Exception:
            log.exception("email_send outcome unknown")
            return f"Error: {UNCONFIRMED}"
        message_id = sent.get("id") if isinstance(sent, dict) else None
        if not isinstance(message_id, str) or not message_id:
            return f"Error: {UNCONFIRMED}"
        try:
            await run_in_db(mark_sent, draft_id, message_id)
        except Exception:
            log.exception("email_send: Gmail accepted the message but the draft could not be marked sent")
        return json.dumps({"sent_message_id": message_id, "thread_id": sent.get("threadId")})

    @staticmethod
    async def _release(draft_id: str) -> None:
        try:
            await run_in_db(release_claim, draft_id)
        except Exception:
            log.exception("email_send: the claim could not be released")
