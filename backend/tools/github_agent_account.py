"""The agent-facing control of the agents' own GitHub account: status, connect, check, sign out.

Removing the account is the person's alone: it returns agent shells to the
person's own GitHub identity, so no action here removes it.
"""

from __future__ import annotations

import json
from typing import Any

from infra.db_async import run_in_db, run_off_db
from tools.base import Tool

ACTIONS = ("status", "connect", "check", "sign_out")
_PROMPT_SECONDS = 15.0
_ACCOUNT_NAME = "Agents"
_PROMPT_NEXT = "retry connect; if it repeats, update GitHub CLI or report its prompt format"
_LOGIN_FAILURE_NEXT = "retry connect; if it exits again, check the GitHub CLI installation"


def _signin_with_next(signin: dict[str, Any]) -> dict[str, Any]:
    """Add the recovery action for a named sign-in failure without vendor output."""
    cause = signin.get("cause")
    if cause == "prompt_unrecognized":
        return signin | {"next": _PROMPT_NEXT}
    if cause == "vendor_login_failed":
        return signin | {"next": _LOGIN_FAILURE_NEXT}
    return signin


def _account_row(account_id: str | None) -> dict | None:
    from connections.vendor_accounts import get_account

    return get_account(account_id) if account_id is not None else None


async def _status(*, verify: bool = False) -> dict[str, Any]:
    from connections import github_agent

    account = await (run_off_db(github_agent.verify_now) if verify else run_in_db(github_agent.view))
    return github_agent.source_state(account) | {"account_id": account.account_id}


async def _connect() -> dict[str, Any]:
    from connections import account_controls, github_agent
    from connections.vendor_accounts import add_account

    account = await run_in_db(github_agent.view)
    if account.state == "ready":
        return await _status()
    if account.cause == "multiple_accounts":
        return await _status()
    account_id = account.account_id
    if account_id is None:
        account_id = (await run_in_db(add_account, github_agent.PROVIDER, _ACCOUNT_NAME))["id"]
    row = await run_in_db(_account_row, account_id)
    if row["state"] == "signed_in":
        # Signed in but not ready (the person's own login, or a rejected token): sign out first.
        return await _status() | {"next": "sign_out, then connect as the agents' account"}
    started = await account_controls.start_signin(account_id)
    if started.get("status") == "waiting":
        started = await account_controls.wait_for_prompt(account_id, timeout=_PROMPT_SECONDS)
    return {"signin": _signin_with_next(started)}


async def _check() -> dict[str, Any]:
    from connections import account_controls, github_agent
    from connections.vendor_accounts import AccountError

    account = await run_in_db(github_agent.view)
    shown: dict[str, Any] = {}
    if account.account_id is not None:
        try:
            shown["signin"] = _signin_with_next(await account_controls.check_signin(account.account_id))
        except AccountError:
            pass  # no sign-in in this process: the account's own state answers
    return shown | {"account": await _status(verify=True)}


async def _sign_out() -> dict[str, Any]:
    from connections import account_controls, github_agent

    account = await run_in_db(github_agent.view)
    if account.account_id is None:
        return await _status()
    row = await run_in_db(_account_row, account.account_id)
    if row["state"] != "signed_in":
        return await _status()
    _, cleanup = await account_controls.sign_out(account.account_id, expected_revision=row["revision"])
    return {"account": await _status(), "cleanup": {"status": cleanup["status"], "cause": cleanup["cause"]}}


class GitHubAgentAccountTool(Tool):
    """Read and drive the agents' own GitHub account through the product's sign-in."""

    @property
    def name(self) -> str:
        return "github_agent_account"

    @property
    def description(self) -> str:
        return (
            "The agents' own GitHub account, which agent shells use for gh and git push. "
            "status: its state (ready, needs_auth, unverified, wrong_account, not_installed), "
            "a named cause, and its login once verified. connect: create it if missing and "
            "start sign-in; returns a user_code and verification_uri for the person to enter "
            "in a browser signed in as the agents' GitHub account, never their own, or a "
            "named cause and next step (skill github, agent-account.md). check: after the person entered the code, read the sign-in and "
            "verify the account; repeat until ready or a named cause. sign_out: sign the "
            "account out; agent shells stay locked until it is connected again. Never run "
            "gh auth login in the shell."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS),
                           "description": "What to do with the agents' account."},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        from connections.vendor_accounts import AccountError

        action = kwargs.get("action")
        if action not in ACTIONS:
            return f"Error: action must be one of {', '.join(ACTIONS)}"
        handlers = {"status": _status, "connect": _connect, "check": _check, "sign_out": _sign_out}
        try:
            result = await handlers[action]()
        except AccountError as exc:
            return f"Error: {exc}"
        return json.dumps(result, sort_keys=True)
