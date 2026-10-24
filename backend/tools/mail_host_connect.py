"""The agent-facing start of an email account on another host: the row and its Keys entry, never a password.

Only the designated CORSIA thread may call it. It takes the address (and server names only for
a host CORSIA doesn't know), adds one row waiting for an app password, and says where the
person makes that password. The person pastes it from that row in Settings › Connected apps; the
password never passes through here or through the agent.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from connections.connected_accounts import ConnectedAccountError
from connections.mailhost import account, store
from connections.mailhost.endpoints import EndpointsRefused
from infra.database import get_db
from infra.db_async import run_in_db
from tools.base import Tool
from tools.corsia_api import ApiCallError, conductor_user

log = logging.getLogger(__name__)

NEXT_WAITING = (
    "Post the app-password page with post_update as a chat link the person opens in their own browser (its click "
    "path is in click_path); never open it yourself and never ask for, read or repeat the password. Then tell them: "
    "on the account's row in Settings › Connected apps, press Set key and paste the app password. CORSIA checks it "
    "and the account shows Connected in Connected apps. {extra}"
)
NEXT_EXISTING = (
    "This address already has an account, so nothing was changed: its servers stay as they were. Status: {status}. "
    "The person fixes it on its row in Settings › Connected apps."
)
NEXT_CONNECTED = "This address is already connected; nothing was changed."


def _connect(thread_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    db = get_db()
    try:
        return account.connect(
            arguments.get("address"), thread_id=thread_id, imap=arguments.get("imap"), smtp=arguments.get("smtp"),
            caldav=arguments.get("caldav"), username=arguments.get("username"), db=db,
        )
    finally:
        db.close()


def _next(result: dict[str, Any]) -> str:
    if not result["already_existed"] or result["status"] == store.WAITING:
        extra = result.get("first_turn_on_imap", "")
        destinations = result.get("password_goes_to")
        if destinations:
            extra = f"{extra} Tell them the app password goes to: {', '.join(destinations)}.".strip()
        return NEXT_WAITING.format(extra=extra).strip()
    if result["status"] in store.CONNECTED_STATUSES:
        return NEXT_CONNECTED
    return NEXT_EXISTING.format(status=result["status"])


class MailHostConnectTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "mail_host_connect"

    @property
    def description(self) -> str:
        return (
            "Add the person's email account on a host other than Gmail or Microsoft (iCloud, Yahoo, Fastmail, AOL, "
            "Zoho, GMX or any IMAP/SMTP server). Pass the address; CORSIA fills in a known host's servers itself. "
            "Pass imap and smtp (host or host:port), and optionally caldav (https address) and username, only for a "
            "host it doesn't know: ask the person for them, since they decide where the password is sent. One account "
            "per address: a second call returns the existing one and never changes its servers. It returns the page "
            "where the person makes an app password; they set it on the account's row in Settings › Connected apps themselves, "
            "so never ask for or handle the password. After that, email_search, email_read and calendar_list take this account_id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        text = {"type": "string"}
        return {
            "type": "object", "additionalProperties": False, "required": ["address"],
            "properties": {
                "address": {"type": "string", "description": "The person's email address on that host."},
                "imap": {**text, "description": "IMAP server for an unknown host, e.g. mail.example.com or mail.example.com:993."},
                "smtp": {**text, "description": "SMTP server for an unknown host, e.g. smtp.example.com or smtp.example.com:587."},
                "caldav": {**text, "description": "CalDAV https address for an unknown host, if it has a calendar."},
                "username": {**text, "description": "Login name when it isn't the address."},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        actor = kwargs.get("_thread_id")
        if not isinstance(actor, str) or not actor:
            return "Error: current thread identity is unavailable; retry from a live turn."
        try:
            await run_in_db(conductor_user, actor)
        except ApiCallError:
            return "Error: Only the designated CORSIA thread can use mail host tools."
        try:
            result = await run_in_db(_connect, actor, kwargs)
        except EndpointsRefused as exc:
            return f"Error: {exc}"
        except ConnectedAccountError:
            return "Error: Couldn't add that account."
        except Exception as exc:
            log.error("mail_host_connect failed inside CORSIA: %s", type(exc).__name__)
            return "Error: Couldn't add that account."
        return json.dumps({**result, "link": f"#connected-apps/{result['account_id']}", "next": _next(result)})
