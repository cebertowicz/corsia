"""Backend-bound shell identities and the Safehouse invocation surface they feed.

Owns the sanitized child environment, the one ``--env-pass`` name list, and the
shell side of the agents' GitHub account: its environment, read-only grant and
the rule hiding the person's own ``gh`` home, all from one view per spawn.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

from connections import github_agent
from connections.github_agent import AgentAccountView
from connections.github_agent_home import BOT_ENV_NAMES, is_host_identity_name
from events.trace_env import TRACE_ENV
from infra.db_async import run_in_db
import tools.agent_shell_tools as agent_shell_tools

_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:^|_)(?:API_?KEY|AUTH_?TOKEN|ACCESS_?TOKEN|TOKEN|SECRET|PASSWORD|"
    r"CREDENTIALS?|PRIVATE_?KEY)(?:_|$)",
    re.IGNORECASE,
)
_ENV_CONTROL_NAMES = frozenset({"BASH_ENV", "ENV", "CDPATH"})
# The backend's own identities Safehouse may pass into a child, in argv order.
# TRACE_ENV is set only by the script runner for a run an event rule started.
_SHELL_PASS_NAMES = ("CORSIA_THREAD_ID", "CORSIA_PROCESS_OWNER_MARKER", "CORSIA_TOOL_INVOCATION_ID",
                     "AGENT_BROWSER_ARGS", *agent_shell_tools.CACHE_ENV_NAMES, TRACE_ENV)


def is_credential_env_name(name: str) -> bool:
    """Whether a variable carries a credential or a GitHub or Git identity no child inherits."""
    return bool(_SENSITIVE_ENV_NAME_RE.search(name)) or is_host_identity_name(name)


def shell_env_pass_names() -> str:
    """The one ``--env-pass`` list: the backend's names, then the agents' account's.

    Safehouse skips a name the environment does not carry, so the values decide what passes.
    """
    return ",".join((*_SHELL_PASS_NAMES, *BOT_ENV_NAMES))


async def bind_agent_account(env: dict[str, str]) -> AgentAccountView:
    """Read the agents' GitHub account once for a spawn and set its shell env from that view.

    The spawn passes the same view to its grant and deny rule, so the three cannot disagree.
    """
    view = await run_in_db(github_agent.view)
    env.update(github_agent.shell_env(view))
    return view


def agent_account_ro_dirs(view: AgentAccountView | None) -> list[Path]:
    """The agents' account home, granted read-only so no command can sign it in or out."""
    return github_agent.sandbox_ro_dirs(github_agent.view() if view is None else view)


def agent_tool_ro_dirs() -> list[Path]:
    """The folders CORSIA's Python and runtimes live in, granted read-only so PATH can reach them."""
    return agent_shell_tools.readable_dirs()


def agent_account_deny_rule(render: Callable[[Path], str], view: AgentAccountView | None) -> str | None:
    """The profile rule hiding the person's own ``gh`` home, or None when it is not in use."""
    hidden = github_agent.default_home_deny(github_agent.view() if view is None else view)
    if hidden is None:
        return None
    return f'(deny file-read* file-write* (subpath "{render(hidden.resolve(strict=False))}"))'


def safehouse_subprocess_env(
    thread_id: str | None, *, invocation_id: str | None = None,
) -> dict[str, str]:
    """Return the host environment Safehouse may use to build a sanitized child.

    Safehouse needs normal host discovery variables such as PATH and
    SSH_AUTH_SOCK. Policy-control variables, provider credentials and the
    host's GitHub and Git identity never reach it; only the agents' account
    sets a shell's GitHub identity. Thread and foreground invocation identities are backend-bound;
    inherited values cannot supply either identity.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if (
            not name.startswith("SAFEHOUSE_")
            and name not in _ENV_CONTROL_NAMES
            and not is_credential_env_name(name)
            and name not in {
                "CORSIA_THREAD_ID", "CORSIA_PROCESS_OWNER_MARKER",
                "CORSIA_TOOL_INVOCATION_ID", TRACE_ENV,
            }
        )
    }
    if thread_id:
        env["CORSIA_THREAD_ID"] = str(thread_id)
    if thread_id and isinstance(invocation_id, str) and invocation_id.strip():
        env["CORSIA_TOOL_INVOCATION_ID"] = invocation_id
    if sys.platform == "darwin":
        # Chromium cannot nest its own Seatbelt sandbox inside Safehouse's.
        env["AGENT_BROWSER_ARGS"] = "--no-sandbox"
    elif sys.platform == "linux":
        env.pop("AGENT_BROWSER_ARGS", None)
    env.update(agent_shell_tools.shell_environment(env.get("PATH", ""), thread_id))
    return env


def strip_external_browser_environment(env: dict[str, str]) -> None:
    """Remove browser import state for a reviewed external child launch."""
    for name in tuple(env):
        if name.startswith("AGENT_BROWSER_"):
            env.pop(name, None)


def shell_identity_refusal(thread_id: object, turn_id: str | None) -> str | None:
    """The refusal for a call without an exact active thread and turn identity, else None."""
    if not isinstance(thread_id, str) or not thread_id or turn_id is None:
        return "Error: shell execution requires an exact active thread and turn identity"
    return None


def shell_execution_context(
    kwargs: dict[str, Any], *, export_invocation: bool,
) -> tuple[str | None, str | None, str | None, dict[str, str]]:
    """Consume registry-owned identities and derive the child's environment.

    A raw Turn id is never authoritative, including direct/test calls.
    Background and long-running launches receive no invocation export.
    """
    from core.turn_core import TurnIdentity

    thread_id = kwargs.pop("_thread_id", None)
    project_id = kwargs.pop("_thread_project_id", None)
    invocation_id = kwargs.pop("_thread_invocation_id", None)
    turn_identity = kwargs.pop("_host_access_turn_identity", None)
    kwargs.pop("_thread_turn_id", None)
    turn_id = turn_identity.turn_id if isinstance(turn_identity, TurnIdentity) else None
    env = safehouse_subprocess_env(
        thread_id, invocation_id=invocation_id if export_invocation else None,
    )
    return thread_id, project_id, turn_id, env
