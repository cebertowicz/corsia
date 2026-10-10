"""The agent-facing read of model sources and the GitHub connection."""

from __future__ import annotations

import json
from typing import Any

from infra.db_async import run_in_db, run_off_db
from tools.base import Tool

_GITHUB_STATES = {"available": "ready", "needs_auth": "needs_auth",
                  "not_installed": "not_installed"}


def github_state() -> dict[str, Any]:
    """GitHub's stored state from the startup seed or the last recheck; never the shell.

    GitHub is not a model source: it sits beside the sources, so nothing that
    counts or preselects a source can see it.
    """
    from connections.models import get_connection

    row = get_connection("github")
    if row is None:
        return {"state": "not_installed"}
    from connections.models import SOURCE_CHECK_KEY

    shown: dict[str, Any] = {"state": _GITHUB_STATES.get(str(row.get("status")), "unverified")}
    check = (row.get("config") or {}).get(SOURCE_CHECK_KEY) or {}
    if check.get("state") == "unverified":
        # A gh too old to read its sign-in: the row's reason is the person's one fix.
        shown = {"state": "unverified", "reason": check.get("reason")}
    return shown


def github_agent_state(*, recheck: bool = False) -> dict[str, Any]:
    """The agents' own GitHub account, never the person's; a recheck waits for its verification."""
    from connections import github_agent

    return github_agent.source_state(github_agent.verify_now() if recheck else github_agent.view())


async def _recheck_agent_account() -> None:
    """Let ``gh`` re-read the agents' home: a definite sign-out there commits the row signed out."""
    from connections import account_controls, github_agent
    from connections.vendor_accounts import AccountError

    account = await run_in_db(github_agent.view)
    if account.account_id is None:
        return
    try:
        await account_controls.verify(account.account_id)
    except AccountError:
        pass  # removed meanwhile: the next view reads it


class ModelSourcesTool(Tool):
    """Return ``GET /api/providers/sources`` plus GitHub and the agents' account, optionally after one recheck."""

    @property
    def name(self) -> str:
        return "model_sources"

    @property
    def description(self) -> str:
        return (
            "Read which model sources (Claude Code, Codex, the local model, "
            "External endpoints) are ready on this computer, each with one "
            "state and the user's one fix, plus whether GitHub is connected "
            "and the state of the agents' own GitHub account (github_agent), "
            "with its login once verified. "
            "Pass recheck=true once when the user says they signed in or a "
            "source reads unverified; it re-runs every check, GitHub's "
            "included, one time. If that recheck errors, read once without "
            "it: those rows are as last checked, not fresh. Never retry."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "recheck": {
                    "type": "boolean",
                    "description": "Re-run every source check once before reading.",
                },
            },
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        from api.provider_sources import list_sources, recheck_sources_once

        if kwargs.get("recheck") is True:
            sources = await recheck_sources_once()
        else:
            sources = await run_in_db(list_sources)
        github = await run_in_db(github_state)
        if kwargs.get("recheck") is True:
            await _recheck_agent_account()
            # Verification runs ``gh``, so it waits off the database pool.
            agent_github = await run_off_db(github_agent_state, recheck=True)
        else:
            agent_github = await run_in_db(github_agent_state)
        return json.dumps({"sources": sources, "github": github, "github_agent": agent_github},
                          sort_keys=True)
