"""The run-private clients a script run gets: its sockets, ``corsia`` client and ``gh`` configuration.

An approved authenticated run gets the owner tool channel (``t``), the GitHub forwarder (``g``) when there is
a login to forward, and, when its approval names saved items, the saved-item forwarder (``s``) and the
given-items socket (``v``). A rule-started run that is not authenticated gets only the repository-scoped
GitHub read channel. Moved out of ``script_sandbox`` unchanged apart from the saved-item sockets.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from automation.script_github_channel import github_channel
from automation.script_github_proxy import github_proxy
from automation.script_item_given import SOCKET_ENV as GIVEN_SOCKET_ENV, given_items
from automation.script_item_proxy import SOCKET_ENV as SEND_SOCKET_ENV, item_proxy
from automation.script_items import RunItems
from automation.script_tool_channel import tool_channel
from connections import github_agent
from connections.github_agent import AgentAccountView
from connections.github_person import person_forwarder_credential
from tools.unix_socket_policy import corsia_temp_root, corsia_temp_root_for_bind


class ScriptSandboxError(RuntimeError):
    """The run could not start; its message is shown to the owner."""


@asynccontextmanager
async def _legacy_github_client(thread_id: str, run_id: str, rule_id: str | None, repo: str):
    """The repository-scoped read channel for rule-started scripts that are not authenticated.

    A bridge with one exit: Agent Substrate deletes it, the read socket, its environment
    name and ``cli_github``'s legacy branch in one change once the scripts that read through
    it (the GitHub screen's) run as authenticated scripts.
    """
    stack = AsyncExitStack()
    try:
        socket = await stack.enter_async_context(github_channel(thread_id, run_id, rule_id, repo=repo))
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="github-client-", dir=corsia_temp_root_for_bind(thread_id))))
        client = directory / "corsia"
        client.write_bytes((Path(__file__).resolve().parents[1] / "cli_github.py").read_bytes())
        client.chmod(0o700)
    except OSError as exc:
        await stack.aclose()
        raise ScriptSandboxError(f"script GitHub channel unavailable: {type(exc).__name__}: {exc}") from exc
    async with stack:
        yield socket, directory


@asynccontextmanager
async def _authenticated_client(thread_id: str, view: AgentAccountView, items: RunItems | None = None):
    """Run-private sockets and a script-readable configuration containing no credential.

    ``items`` are the saved items this run may use; their sockets are entered after ``g``, so they close
    first, and the environment naming them is yielded last. A withheld run gets neither socket.
    """
    async with AsyncExitStack() as stack:
        socket_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="r", dir=corsia_temp_root())))
        tool_socket = socket_dir / "t"
        await stack.enter_async_context(tool_channel(tool_socket, thread_id))
        client_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="script-client-", dir=corsia_temp_root_for_bind(thread_id))))
        client = client_dir / "corsia"
        client.write_bytes((Path(__file__).resolve().parents[1] / "cli_github.py").read_bytes())
        client.chmod(0o700)
        config_dir = None
        github_socket = None
        if view.ready or (view.absent and await asyncio.to_thread(_host_login_available)):
            from events.connectors.github_read_budget import budget, person_budget

            # With no agents' account an approved script reads as the host gh login, as agent shells do.
            credential = ((lambda: github_agent.script_forwarder_credential(view)) if view.ready
                          else person_forwarder_credential)
            github_socket = socket_dir / "g"
            await stack.enter_async_context(github_proxy(
                github_socket, credential, read_budget=budget if view.ready else person_budget,
                hide=items is not None,
            ))
            config_dir = client_dir / "gh"
            config_dir.mkdir(mode=0o700)
            (config_dir / "config.yml").write_text(f"http_unix_socket: {github_socket}\n")
            # gh won't run without a stored login; the forwarder adds the real one. The key is split
            # so credential scanners don't take this placeholder for a secret.
            (config_dir / "hosts.yml").write_text(
                "github.com:\n    user: script\n    oauth_" + "token: placeholder\n"
            )
        item_env: dict[str, str] = {}
        if items is not None and not items.withheld:
            if items.sendable():
                await stack.enter_async_context(item_proxy(socket_dir / "s", items, github_socket=github_socket))
                item_env[SEND_SOCKET_ENV] = str(socket_dir / "s")
            if items.given():
                await stack.enter_async_context(given_items(socket_dir / "v", items))
                item_env[GIVEN_SOCKET_ENV] = str(socket_dir / "v")
        yield socket_dir, tool_socket, config_dir, client_dir, item_env


def _host_login_available() -> bool:
    """Whether the host ``gh`` login can serve a forwarder; the credential read is discarded."""
    try:
        person_forwarder_credential()
    except ValueError:
        return False
    return True
