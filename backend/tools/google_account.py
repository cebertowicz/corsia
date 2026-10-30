"""The agent-facing Google sign-in: add an account, or read the state of the accounts that reach the caller, never a token.

Only a designated coordinating thread (the check ``corsia_api`` uses) may call
these, and it sees only the accounts ``account_reaches_thread`` lets its thread
see, the same set ``/api/google/status`` gives it. The sign-in itself happens
in the person's own browser; the token goes from Google into that account's
bundle and never reaches a tool result. An added account reaches the scope of
the thread that added it, never wider.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from connections.connected_accounts import GOOGLE_PROVIDER, resolve_credentials, visible_account
from connections.google import oauth
from connections.google.client import GoogleError, STATUS_CONNECTED
from infra.database import get_db
from infra.db_async import run_in_db
from tools.base import Tool
from tools.corsia_api import ApiCallError, conductor_user

log = logging.getLogger(__name__)


def _reach_phrase(account: dict[str, object], db) -> str:  # type: ignore[no-untyped-def]
    """Describe an unambiguous reach the person can recognize in Connected apps."""
    reach = account["reach"]
    if not isinstance(reach, dict):
        return ""
    if reach.get("mode") == "all":
        return " for all your projects"
    project_ids = reach.get("project_ids")
    if (
        reach.get("mode") != "selected"
        or reach.get("outside_projects")
        or not isinstance(project_ids, list)
        or len(project_ids) != 1
        or not isinstance(project_ids[0], str)
    ):
        return ""
    project = db.execute("SELECT name FROM projects WHERE id=?", (project_ids[0],)).fetchone()
    return f" for {project['name']}" if project is not None else ""



def _existing_account_reply(account_id: str, thread_id: str) -> tuple[str, str]:
    """Build the row-directed B-G1 tool result without starting another OAuth flow.

    An account outside the caller's reach gets the unknown-id refusal, so the caller
    can't tell it exists.
    """
    db = get_db()
    try:
        account = visible_account(account_id, thread_id, db=db, provider=GOOGLE_PROVIDER)
        if account is None:
            raise oauth.GoogleSignInError(oauth.UNKNOWN_ACCOUNT)
        if account["state"] == STATUS_CONNECTED and resolve_credentials(account_id, db=db) is not None:
            reply = oauth.not_covered_reply(account, thread_id, db) or oauth.ALREADY_CONNECTED.format(
                address=account["address"], reach=_reach_phrase(account, db),
            )
        else:
            reply = oauth.ALREADY_NEEDS_SIGN_IN.format(address=account["address"])
        return reply, f"#connected-apps/{account['id']}"
    finally:
        db.close()


async def _account_reply(account_id: str, thread_id: str) -> tuple[str, str]:
    return await run_in_db(_existing_account_reply, account_id, thread_id)


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
            "calling google_connect. Without account_id, this tool starts the person's Google sign-in and "
            "returns a URL for the person to open; they sign in and click Allow in their own browser, and "
            "the token never reaches you. With account_id, it returns {reply, link} for that existing "
            "account: post its reply verbatim with post_update followed by [Open it in Connected apps](<link>); "
            "its Connected apps row provides the next action. Omit account_id to add another account, which agents "
            "can use where this conversation works. "
            "By default, sign-in and token renewal go through Google's Workspace extension sign-in service, "
            "with one Allow and no Google Cloud project. Google asks for Gmail, Calendar, Drive and Docs; "
            "using their own Google Cloud project also asks for Sheets. Gmail "
            "and Calendar are required; the others are optional, and google_status lists what "
            "they allowed. The Google tools here use email and calendar. To use their own "
            "Desktop OAuth client instead, pass its downloaded JSON as client_file; "
            "skill `google-setup` covers that backup path."
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
                    "description": "An account_id from google_status. Post its returned reply verbatim with post_update followed by [Open it in Connected apps](<link>); omit to add an account.",
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
            if account_id:
                reply, link = await _account_reply(account_id.strip(), thread_id)
                return json.dumps({"reply": reply, "link": link})
            url, signing_in = await oauth.start_sign_in(
                kwargs.get("client_file"), actor=("agent", thread_id),
                adding_thread_id=thread_id, starting_thread_id=thread_id,
            )
        except GoogleError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("google_connect failed")
            return "Error: Couldn't start the Google sign-in."
        return json.dumps({
            "url": url, "account_id": signing_in,
            "next": (
                "Give the person this link to open and sign in. CORSIA wakes you once, when they finish "
                f"or when the link expires after {oauth.PENDING_SECONDS // 60} minutes; then call google_status."
            ),
        })


class GoogleStatusTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "google_status"

    @property
    def description(self) -> str:
        return (
            "Every connected Google account this conversation can use: {accounts: [{account_id, email, status (connected, "
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
            return json.dumps(await run_in_db(oauth.accounts_status, kwargs["_thread_id"]))
        except Exception:
            log.exception("google_status failed")
            return "Error: Couldn't read the Google sign-in state."
