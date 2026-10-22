"""A ``review`` launch of the stock Claude CLI loads nothing it was not given.

A review child reads repository paths through ``--add-dir`` and runs inside the
workspace, under the workspace's and project's own CLAUDE.md files and
``.claude/`` settings. This plants an instruction file, import, rule, memory
index and hook at every level, including the reviewed directory, sets the
switch that makes add-dir CLAUDE.md files load in the ambient environment, and
launches the installed CLI with the command and environment ``CCStreamSession``
builds for a review child. An ungoverned arm proves the planted sources load.

The read arms script the model's first reply as one tool call and check what
the CLI sends back: a review child reads its add-dirs, and nothing outside
its working directory and add-dirs reaches the model (event-rules design
§8.1). That rests on the CLI's ``--restricted`` flag, which confines its file
tools to the working directories; no sandbox wraps the child.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from cc_user_context_fixtures import InferencePeer, seed_user_context, serve
from contract.matrix_environment import guarded_command
from subagents.cc_invocation import CONFINEMENT_REVIEW, build_claude_cmd
from subagents.cc_stream import CCStreamSession

_ADD_DIR_SWITCH = "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"
_ADD_DIR_SENTINEL = "SENTINEL_ADD_DIR_CLAUDE_MD_7f9"
_HOOK_SENTINEL = "SENTINEL_HOOK_OUTPUT_7fa"
_OUTSIDE_SECRET = "outside-secret-9d1f3a7c"
_INSIDE_TEXT = "inside-visible-5c2e"

ToolCall = Callable[[Path, Path], tuple[str, dict]]


def _plant(base: Path, root: Path) -> tuple[Path, Path, Path]:
    """Plant every source; return the cwd, the reviewed directory and the hook marker."""
    cwd = seed_user_context(base, root)
    reviewed = base / "reviewed"
    (reviewed / ".claude").mkdir(parents=True)
    (reviewed / "CLAUDE.md").write_text(_ADD_DIR_SENTINEL + "\n")
    (reviewed / "inside.txt").write_text(_INSIDE_TEXT + "\n")
    (base / "secret.txt").write_text(_OUTSIDE_SECRET + "\n")
    (base / "tmp").mkdir()
    marker = base / "hook-ran"
    hook = {"hooks": [{"type": "command", "command": f"touch {marker}; echo {_HOOK_SENTINEL}"}]}
    settings = json.dumps({"hooks": {"SessionStart": [hook], "UserPromptSubmit": [hook]}})
    for directory in (root, cwd / ".claude", cwd.parent / ".claude", reviewed / ".claude"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "settings.json").write_text(settings)
        (directory / "settings.local.json").write_text(settings)
    return cwd, reviewed, marker


def _launch(binary: str, cwd: Path, reviewed: Path, root: Path, arm: str):
    """Return the command and base environment the named arm launches with."""
    if arm == "ungoverned":
        command = [binary, "-p", "--output-format", "json", "--add-dir", str(reviewed)]
        return command, {_ADD_DIR_SWITCH: "1"}
    session = CCStreamSession(
        binary=binary, session_id="00000000-0000-4000-8000-000000000001",
        model="claude-sonnet-4-6", working_dir=str(cwd), claude_dir=str(root),
        confinement=CONFINEMENT_REVIEW, add_dirs=(str(reviewed),),
    )
    # The prompt goes over stdin: ``--add-dir`` is variadic and would take it.
    return build_claude_cmd(session.invocation(), stream_input=False), session._build_env()


def _leaked(peer: InferencePeer) -> list[str]:
    joined = "\n".join(peer.bodies)
    extra = [name for name, value in (("add_dir", _ADD_DIR_SENTINEL), ("hook", _HOOK_SENTINEL))
             if value in joined]
    return sorted(peer.leaked() + extra)


async def _run(arm: str, tool_call: ToolCall | None = None) -> tuple[InferencePeer, bool]:
    """Run the installed CLI; return the peer that saw its requests and whether a hook ran."""
    binary = shutil.which("claude")
    assert binary, "stock Claude CLI is required for this explicitly selected integration test"
    base = Path(tempfile.mkdtemp(prefix="cc-review-context-")).resolve()
    try:
        root = base / "home" / ".claude"
        cwd, reviewed, marker = _plant(base, root)
        peer = InferencePeer(tool_call(base, reviewed) if tool_call else None)
        server, port = await serve(peer)
        command, env = _launch(binary, cwd, reviewed, root, arm)
        env |= {
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(base / "home"),
            "TMPDIR": str(base / "tmp"), "CLAUDE_CONFIG_DIR": str(root),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_API_KEY": "loopback-test-key", "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
        process = await asyncio.create_subprocess_exec(
            *guarded_command(command, port), cwd=str(cwd), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(b"say ok\n"), 120)
        finally:
            server.close()
            await server.wait_closed()
        assert peer.bodies, f"CLI sent no request: {stderr.decode()[-400:]}"
        return peer, marker.exists()
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.integration
async def test_review_launch_loads_no_instructions_hooks_or_memory(monkeypatch):
    monkeypatch.setenv(_ADD_DIR_SWITCH, "1")

    peer, hook_ran = await _run("review")

    assert _leaked(peer) == []
    assert hook_ran is False
    assert peer.memory_prompt_present() is False


@pytest.mark.integration
async def test_ungoverned_launch_proves_the_planted_sources_load():
    peer, hook_ran = await _run("ungoverned")

    assert {"user_claude_md", "add_dir"} <= set(_leaked(peer))
    assert hook_ran is True


def _tool_results(peer: InferencePeer) -> list[str]:
    return [body for body in peer.bodies if '"tool_result"' in body]


def _read_outside(base: Path, _reviewed: Path) -> tuple[str, dict]:
    return "Read", {"file_path": str(base / "secret.txt")}


def _grep_outside(base: Path, _reviewed: Path) -> tuple[str, dict]:
    # The pattern reaches the model as the call's input; only a match could carry the secret.
    return "Grep", {"pattern": "outside-secret-[0-9a-f]+", "path": str(base),
                    "output_mode": "content"}


@pytest.mark.integration
@pytest.mark.parametrize("tool_call", [_read_outside, _grep_outside], ids=["read", "grep"])
async def test_review_launch_returns_nothing_from_outside_its_directories(tool_call):
    peer, _hook_ran = await _run("review", tool_call)

    assert _tool_results(peer), "the CLI never answered the scripted tool call"
    assert not any(_OUTSIDE_SECRET in body for body in peer.bodies)


@pytest.mark.integration
async def test_review_launch_reads_inside_its_add_dir():
    peer, _hook_ran = await _run(
        "review", lambda _base, reviewed: ("Read", {"file_path": str(reviewed / "inside.txt")}),
    )

    assert any(_INSIDE_TEXT in body for body in _tool_results(peer))
