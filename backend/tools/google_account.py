"""The agent-facing Google sign-in: start it and read its state, never a token.

Only the designated CORSIA thread (the check ``corsia_api`` uses) may call
these. The sign-in itself happens in the person's own browser; the token goes
from Google into the key store and never reaches a tool result.
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
            "Call google_status first. If it returns needs_action, follow it before calling "
            "google_connect. If it says not_connected, this tool starts the person's Google sign-in "
            "for email and calendar and returns a URL for the person to open; they sign in and click "
            "Allow in their own browser, and the token never reaches you. Pass client_file, the path "
            "of the Desktop app OAuth client JSON they downloaded from Google Cloud, unless a client "
            "was saved by an earlier sign-in."
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
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        try:
            url = await oauth.start_sign_in(kwargs.get("client_file"))
        except GoogleError as exc:
            return f"Error: {exc}"
        except Exception:
            log.exception("google_connect failed")
            return "Error: Couldn't start the Google sign-in."
        return json.dumps({
            "url": url,
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
            "The Google sign-in's state: email, granted scopes, status (connected, needs_sign_in "
            "or not_connected), poller_state and last_success_at. When sign-in is rejected, "
            "needs_action gives the recovery steps."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        if refusal := await _designated(kwargs):
            return refusal
        try:
            return json.dumps(await run_in_db(oauth.account_status))
        except Exception:
            log.exception("google_status failed")
            return "Error: Couldn't read the Google sign-in state."
