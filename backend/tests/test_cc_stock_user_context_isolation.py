"""A managed stock Claude CLI must load nothing CORSIA did not deliver (#1874).

The launch arguments and spawn environment come from the production helpers, so
a future settings or environment change that re-opens user instruction files or
auto memory fails here instead of silently reaching the model.

Three arms, each named for the mechanism it exercises:

* ``managed`` - ``claude_project_discovery_args``: settings excludes plus the
  environment switch. Live sessions, checkpoint forks (built through
  ``build_claude_cmd``) and catalog/context discovery launch this way.
* ``settings_free`` - ``--setting-sources ""``, what the reviewer and isolated
  structured inference launch: no settings source is read, so instruction files
  never load, and ``CLAUDE_CODE_DISABLE_AUTO_MEMORY`` alone stops memory.
* ``ungoverned`` - no CORSIA controls, proving the sentinels are reachable and
  neither governed arm can pass vacuously.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from cc_user_context_fixtures import InferencePeer, seed_user_context, serve
from contract.matrix_environment import guarded_command
from subagents.cc_env import clean_claude_env
from subagents.managed_launch import claude_project_discovery_args

_BASE_ARGS = [
    "-p", "say ok", "--output-format", "json", "--model", "claude-sonnet-4-6",
    "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
]
_SETTINGS_FREE_ARGS = ["--setting-sources", ""]


def _spawn_env(root: Path, home: Path, base: Path, port: int, arm: str) -> dict[str, str]:
    """Build the spawn environment exactly as the named launch path would.

    ``CLAUDE_CONFIG_DIR`` is pinned to the seeded root in every arm: production
    binds it per session, and inheriting an ambient one would point the governed
    arms at a root holding none of the sentinels — a vacuous pass the ungoverned
    control could not detect.
    """
    env = {} if arm == "ungoverned" else clean_claude_env()
    return env | {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
        "HOME": str(home),
        "TMPDIR": str(base),
        "CLAUDE_CONFIG_DIR": str(root),
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_API_KEY": "corsia-fixture-not-a-credential",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    }


def _launch_args(arm: str) -> list[str]:
    """Return the arguments the named launch path adds to a stock invocation."""
    if arm == "managed":
        return claude_project_discovery_args()
    return _SETTINGS_FREE_ARGS if arm == "settings_free" else []


async def _run_stock_claude(arm: str) -> InferencePeer:
    """Run the installed CLI against a loopback peer with sentinels planted."""
    binary = shutil.which("claude")
    assert binary, "stock Claude CLI is required for this explicitly selected integration test"
    base = Path(tempfile.mkdtemp(prefix="cc-user-context-")).resolve()
    try:
        home = base / "home"
        home.mkdir()
        root = home / ".claude"
        cwd = seed_user_context(base, root)
        peer = InferencePeer()
        server, port = await serve(peer)
        command = guarded_command([binary, *_BASE_ARGS, *_launch_args(arm)], port)
        process = await asyncio.create_subprocess_exec(
            *command, cwd=str(cwd), env=_spawn_env(root, home, base, port, arm),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), 120)
        finally:
            server.close()
            await server.wait_closed()
        assert peer.bodies, f"CLI sent no request: {stderr.decode()[-400:]}"
        return peer
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.integration
async def test_managed_launch_delivers_no_user_instructions_or_memory():
    peer = await _run_stock_claude("managed")

    assert peer.leaked() == []
    assert peer.memory_prompt_present() is False


@pytest.mark.integration
async def test_empty_setting_sources_load_no_instructions_and_env_switch_stops_memory():
    peer = await _run_stock_claude("settings_free")

    assert peer.leaked() == []
    assert peer.memory_prompt_present() is False


@pytest.mark.integration
async def test_ungoverned_launch_proves_the_sentinels_are_reachable():
    peer = await _run_stock_claude("ungoverned")

    assert "user_claude_md" in peer.leaked()
    assert peer.memory_prompt_present() is True
