"""``google_workspace``: one gws command on a connected Google account (Docs, Sheets, Drive, Calendar).

CORSIA previews each command first (gws's ``--dry-run``, no sign-in at all), checks the one request
it prints (``tools.gws_check``), then runs the real command with the account's access token handed
only to gws's own process. Deletes are made recoverable (``tools.gws_deletions``).
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import Any

from connections.account_errors import AccountError
from connections.account_resolver import Refusals, resolve
from connections.connected_accounts import GOOGLE_PROVIDER
from connections.google import accounts as google_accounts
from connections.google import client
from infra.db_async import run_in_db
from tools import gws_check, gws_messages
from tools.base import Tool
from tools.filesystem_gate import resolve_path
from tools.gws_deletions import NotSaved, failure_text, log_dir, save_calendar, save_event
from tools.gws_run import Completed, GwsUnavailable, build_args, run_gws

log = logging.getLogger(__name__)

_NAME_AN_ACCOUNT = gws_messages.ACCOUNT.replace("{addresses}", "{choices}")
_REFUSALS = Refusals(
    client.NOT_CONNECTED, google_accounts.NONE_AVAILABLE, _NAME_AN_ACCOUNT, _NAME_AN_ACCOUNT,
)
TRASHED = "Moved to the trash, not deleted: Google keeps it for 30 days and the person can restore it."
DELETED_EVENT = "Deleted. The event was saved first as {path}."
CLEARED = "Cleared. {count} events were saved first in {folder}."
_PATH_FLAGS = ("--upload", "--output")


def _account(selector: object, thread_id: object) -> dict:
    return resolve(
        selector, thread_id, providers=(GOOGLE_PROVIDER,), usable_for=google_accounts.usable,
        refusals=_REFUSALS, error_type=client.GoogleError,
    )


def _text(done: Completed) -> str:
    return done.stdout.strip() if done.code == 0 else f"Error: {failure_text(done)}"


class GoogleWorkspaceTool(Tool):
    self_describing_errors = True

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace

    @property
    def name(self) -> str:
        return "google_workspace"

    @property
    def description(self) -> str:
        return gws_messages.DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {
                "account": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "params": {"type": "object"},
                "body": {"type": "object"},
                "upload": {"type": "string"},
                "output": {"type": "string"},
            },
            "required": ["args"],
        }

    async def _workspace_path(self, path: object, *, for_write: bool) -> str | None:
        if not isinstance(path, str) or not path:
            return None
        try:
            resolved = await asyncio.to_thread(resolve_path, path, self._workspace, for_write=for_write)
        except PermissionError:
            raise ValueError(gws_messages.PATHS) from None
        if self._workspace is not None and not resolved.is_relative_to(self._workspace.resolve()):
            raise ValueError(gws_messages.PATHS)
        return str(resolved)

    async def execute(self, **kwargs: Any) -> str:
        args = kwargs.get("args")
        if not isinstance(args, list) or not args or not all(isinstance(word, str) for word in args):
            return "Error: args must be the words after gws, for example [\"drive\",\"files\",\"list\"]."
        if "auth" in args:
            return gws_messages.NO_PREVIEW
        if any(word.startswith(_PATH_FLAGS) for word in args):
            return gws_messages.PATHS
        try:
            account = await run_in_db(_account, kwargs.get("account"), kwargs.get("_thread_id"))
            upload = await self._workspace_path(kwargs.get("upload"), for_write=False)
            output = await self._workspace_path(kwargs.get("output"), for_write=True)
        except (AccountError, ValueError) as error:
            return str(error)
        words = build_args(args, kwargs.get("params"), kwargs.get("body"), upload, output)
        try:
            return await self._run(account, args, words, kwargs)
        except GwsUnavailable as error:
            return str(error)
        except NotSaved as error:
            return str(error)
        except AccountError as error:
            return str(error)
        except Exception:
            log.exception("google_workspace failed")
            return "Error: Couldn't run the Google Workspace command. Try again later."

    async def _run(self, account: dict, args: list[str], words: list[str], kwargs: dict[str, Any]) -> str:
        account_id = str(account["id"])
        if args[0] == "schema" or "--help" in args or "-h" in args:
            return _text(await run_gws(words, account_id=account_id, token=None, workdir=self._workspace))
        previewed = await run_gws([*words, "--dry-run"], account_id=account_id, token=None, workdir=self._workspace)
        preview = gws_check.parse_preview(previewed.stdout)
        if preview is None:
            return gws_messages.NO_PREVIEW
        decision = gws_check.decide(preview)
        if decision.refusal:
            return decision.refusal
        token = await client.access_token(account_id)
        run = partial(run_gws, account_id=account_id, token=token, workdir=self._workspace)
        if decision.kind == gws_check.TRASH_FILE:
            params = {**(kwargs.get("params") or {}), "fileId": decision.file_id}
            done = await run(build_args(["drive", "files", "update"], params, {"trashed": True}, None, None))
            return f"{TRASHED}\n{_text(done)}" if done.code == 0 else _text(done)
        base = self._workspace or Path()
        folder = log_dir(base, str(account["address"]))
        if decision.kind == gws_check.DELETE_EVENT:
            saved = await save_event(run, folder, decision.calendar_id or "", decision.event_id or "")
            done = await run(words)
            return DELETED_EVENT.format(path=saved.relative_to(base)) if done.code == 0 else _text(done)
        if decision.kind == gws_check.CLEAR_CALENDAR:
            saved_all = await save_calendar(run, folder, decision.calendar_id or "")
            done = await run(words)
            return CLEARED.format(count=len(saved_all), folder=folder.relative_to(base)) if done.code == 0 else _text(done)
        return _text(await run(words)) or "Done."
