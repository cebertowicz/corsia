"""The agent-facing Google sign-in: add or sign in one account and read every account's state, never a token.

Only the designated CORSIA thread (the check ``corsia_api`` uses) may call
these. The sign-in itself happens in the person's own browser; the token goes
from Google into that account's bundle and never reaches a tool result. An
added account reaches the scope of the thread that added it, never wider.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from connections.google import oauth
from connections.google.client import GoogleError
from infra.db_async import run_in_db
from tools.base import Tool
from tools.corsia_api import ApiCallError, conductor_user

log = logging.getLogger(__name__)


async def _designated(kwargs: dict[str, Any]) -> str | None:
    """The refusal text when this thread is not the designated CORSIA thread, else None."""
    actor = kwargs.get("_thread_id")
    if not isinstance(actor, str) or not actor:
        return "Error: current thread identity is unavailable; retry from a live turn."
    try:
        await run_in_db(conductor_user, actor)
    except ApiCallError:
        return "Error: Only the designated CORSIA thread can use Google tools."
    return None


class GoogleConnectTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "google_connect"

    @property
    def description(self) -> str:
        return (
            "Call google_status first. If an account there returns needs_action, follow it before "
            "calling google_connect. This tool starts the person's Google sign-in and returns a URL "
            "for the person to open; they sign in and click Allow in their own browser, and the "
            "token never reaches you. Pass account_id to sign an existing account in again; omit "
            "it to add another account, which agents can use where this conversation works. "
            "Google asks them to allow Gmail, Calendar, Drive, Docs and Sheets in one go. Gmail "
            "and Calendar are required; the others are optional, and google_status lists what "
            "they allowed. The Google tools here use email and calendar. Pass client_file, the "
            "path of the Desktop app OAuth client JSON they downloaded from Google Cloud, unless "
            "a client was saved by an earlier sign-in. To set Google up from the start, follow "
            "skill `google-setup`."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "client_file": {
                    "type": "string",
                    "description": "Path of the downloaded Desktop app OAuth client JSON; read by the backend.",
                },
                "account_id": {
                    "type": "string",
                    "description": "An account_id from google_status to sign in again; omit to add an account.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        thread_id = kwargs["_thread_id"]
        account_id = kwargs.get("account_id")
        if account_id is not None and (not isinstance(account_id, str) or not account_id.strip()):
            return f"Error: {oauth.UNKNOWN_ACCOUNT}"
        try:
            url, signing_in = await oauth.start_sign_in(
                kwargs.get("client_file"), actor=("agent", thread_id),
                account_id=account_id.strip() if account_id else None,
                adding_thread_id=None if account_id else thread_id,
            )
        except GoogleError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("google_connect failed")
            return "Error: Couldn't start the Google sign-in."
        return json.dumps({
            "url": url, "account_id": signing_in,
            "next": "Give the person this link to open and sign in, then call google_status.",
        })


class GoogleStatusTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "google_status"

    @property
    def description(self) -> str:
        return (
            "Every connected Google account: {accounts: [{account_id, email, status (connected, "
            "needs_sign_in or signed_out), scopes, reach, poller_state, last_success_at}]}. An "
            "empty list means none is connected. The Gmail and Calendar tools take one of these "
            "as account_id. When an account's sign-in is rejected, its needs_action gives the "
            "recovery steps."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        try:
            return json.dumps(await run_in_db(oauth.accounts_status))
        except Exception:
            log.exception("google_status failed")
            return "Error: Couldn't read the Google sign-in state."
