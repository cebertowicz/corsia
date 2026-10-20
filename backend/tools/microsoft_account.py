"""Agent-facing Microsoft account sign-in tools; never return secrets or provider text."""
from __future__ import annotations

import json
import logging
from typing import Any

from connections.microsoft import oauth
from connections.microsoft.errors import INTERNAL, MicrosoftError
from infra.db_async import run_in_db
from tools.base import Tool
from tools.corsia_api import ApiCallError, conductor_user

log = logging.getLogger(__name__)


async def _designated(kwargs: dict[str, Any]) -> str | None:
    actor = kwargs.get("_thread_id")
    if not isinstance(actor, str) or not actor:
        return "Error: current thread identity is unavailable; retry from a live turn."
    try:
        await run_in_db(conductor_user, actor)
    except ApiCallError:
        return "Error: Only the designated CORSIA thread can use Microsoft tools."
    return None


class MicrosoftConnectTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "microsoft_connect"

    @property
    def description(self) -> str:
        return (
            "Start the person's Microsoft sign-in for their own app registration. Pass the Application (client) ID, "
            "kind (personal, work, or both), and Directory (tenant) ID for work only. All IDs must be GUIDs. "
            "It returns a link for the person to open in their default browser; they sign in and accept there, and "
            "the authorization code and tokens never reach you. Use microsoft_status after they return. To sign an "
            "account in again, pass only its account_id; it reuses that account's registration."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "account_id": {"type": "string", "description": "An existing account to sign in again; omit to add one."},
                "client_id": {"type": "string", "description": "Microsoft Application (client) ID GUID, required to add."},
                "kind": {"type": "string", "enum": ["personal", "work", "both"], "description": "Required to add."},
                "tenant_id": {"type": "string", "description": "Directory (tenant) ID GUID, required for work."},
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        thread_id = kwargs["_thread_id"]
        try:
            url, account_id = await oauth.start_sign_in(
                kwargs.get("client_id"), kwargs.get("kind"), kwargs.get("tenant_id"), actor=("agent", thread_id),
                account_id=kwargs.get("account_id") or None, adding_thread_id=thread_id,
            )
        except MicrosoftError as exc:
            return f"Error: {exc.cause}: {exc.message}"
        except Exception as exc:
            log.error("microsoft_connect failed inside CORSIA: %s", type(exc).__name__)
            return f"Error: {INTERNAL}: {MicrosoftError(INTERNAL).message}"
        return json.dumps({"url": url, "account_id": account_id, "next": "Give the person this link to open, then call microsoft_status."})


class MicrosoftStatusTool(Tool):
    self_describing_errors = True

    @property
    def name(self) -> str:
        return "microsoft_status"

    @property
    def description(self) -> str:
        return "With no account_id, list every Microsoft account this conversation can use. If there are none, return status not_connected and the latest fixed sign-in failure cause when available. With account_id, report that one row: address, kind, state, fixed cause when blocked, and reach."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "properties": {"account_id": {"type": "string", "description": "Optional account_id returned by microsoft_connect or a previous microsoft_status result."}}}

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        try:
            return json.dumps(await run_in_db(oauth.accounts_status, kwargs["_thread_id"], kwargs.get("account_id")))
        except Exception as exc:
            log.error("microsoft_status failed inside CORSIA: %s", type(exc).__name__)
            return f"Error: {INTERNAL}: Couldn't read Microsoft accounts. Try again."
