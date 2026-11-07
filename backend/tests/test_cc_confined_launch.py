"""A ``untrusted`` Claude Code child launches read-only, every time it launches.

The eager spawn and the cold start after the live handle is gone both read
the confinement from the stored launch record; an unconfined launch is
unchanged byte for byte.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cc_runtime_fixtures import (  # noqa: F401
    _seed_parent,
    _wait,
    cc_target,
    child_db,
    fake_claude,
    feed_with_persisted_input,
)
from claude_controls import confined_claude_controls
from subagents.cc_invocation import (
    ClaudeInvocation,
    build_claude_cmd,
    claude_invocation_from_metadata,
    claude_invocation_metadata,
    confined_claude_tool_names,
)
from subagents.managed_launch import claude_project_discovery_args

_OLD_FIELDS = {
    "binary", "permission_mode", "session_id", "resume_session_id",
    "seed_file", "mcp_config", "working_dir", "model", "effort",
}


@pytest.fixture
def reviewed_dir():
    """A directory inside the workspace, so the parent may read it."""
    from infra.config import WORKSPACE

    path = Path(WORKSPACE) / f"review-src-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    yield str(path.resolve())
    shutil.rmtree(path, ignore_errors=True)


async def _retire(child_id: str) -> None:
    """Retire the child and reap its turn-end driver, which would otherwise wake the
    parent after the test ends; the parent's wake is not what these tests check."""
    import subagents.cc_runtime as rt
    from tests.conftest import _join_terminal_driver

    live = rt._LIVE.get(child_id)
    await rt.retire(child_id)
    await _join_terminal_driver(live.session if live else None)


@pytest.fixture
def launches(monkeypatch):
    """Record each real CC launch's command, cwd, env and tool bridge."""
    from subagents.cc_stream import CCStreamSession

    seen: list[dict] = []
    original = CCStreamSession.start

    async def start(session):
        seen.append({
            "cmd": session.build_cmd(), "cwd": session.working_dir,
            "env": session._build_env(), "mcp_config": session.mcp_config,
        })
        await original(session)

    monkeypatch.setattr(CCStreamSession, "start", start)
    return seen


def _stored_invocation(db, thread_id: str) -> dict:
    row = db.execute(
        "SELECT i.metadata FROM artifact_identities i "
        "JOIN artifact_registrations r ON r.artifact_id=i.id "
        "WHERE r.parent_thread_id=? AND i.integration='claude-code' "
        "AND i.type='session' AND i.state='active'",
        (thread_id,),
    ).fetchone()
    return json.loads(row["metadata"])["claude_invocation"]


def _assert_confined(launch: dict, child_id: str) -> None:
    cmd = launch["cmd"]
    profile = confined_claude_controls(mcp_tools=True)
    assert "--safe-mode" not in cmd  # it would drop the bridge's --mcp-config server
    start = cmd.index(profile[0])
    assert cmd[start:start + len(profile)] == profile
    assert cmd[cmd.index("--tools") + 1] == ",".join(confined_claude_tool_names())
    # dontAsk refuses an MCP tool with no allow rule, so each offered read is allowed.
    allowed = cmd.index("--allowedTools") + 1
    assert cmd[allowed:allowed + len(confined_claude_tool_names())] == confined_claude_tool_names()
    assert "--add-dir" not in cmd and "bypassPermissions" not in cmd
    assert cmd.count("--permission-mode") == 1
    # Its reads are the bridge's, so the paths never reach the command line.
    assert cmd[cmd.index("--mcp-config") + 1] == launch["mcp_config"]
    assert launch["mcp_config"] and Path(launch["mcp_config"]).is_file()
    assert launch["cwd"].endswith(f"{child_id}/confined")
    assert Path(launch["cwd"]).is_dir() and not any(Path(launch["cwd"]).iterdir())
    assert "GH_TOKEN" not in launch["env"]
    assert "CORSIA_DELEGATE_TOKEN" not in launch["env"]


@pytest.mark.asyncio
async def test_review_spawn_and_cold_start_stay_confined(
    child_db, fake_claude, launches, reviewed_dir, monkeypatch, tmp_path,
):
    """(i) the eager spawn and (ii) the cold start after retirement are confined."""
    import connections.models as cm
    import subagents.cc_runtime as rt

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("GH_TOKEN", "must-not-reach-the-child")
    monkeypatch.setenv("CORSIA_DELEGATE_TOKEN", "must-not-reach-the-child")
    monkeypatch.setattr(
        cm, "get_connection", lambda provider, db=None: {"config": {"binary": fake_claude}},
    )
    _seed_parent(child_db)
    child_id = await rt.spawn(
        "parent", "review this", binary=fake_claude, target=cc_target("sonnet"),
        broadcast=lambda _sid, _message: None,
        confinement="untrusted", read_dirs=(reviewed_dir,),
    )
    assert await _wait(lambda: rt.is_live(child_id))
    assert len(launches) == 1
    _assert_confined(launches[0], child_id)
    stored = _stored_invocation(child_db, child_id)
    assert stored["confinement"] == "untrusted" and "add_dirs" not in stored
    # The read dirs live only on its row, which every launch reads and the boundary
    # enforces, so a switch to another harness can't launch it unconfined.
    from infra.thread_confinement import read_dirs
    assert read_dirs(child_id, child_db) == (reviewed_dir,)

    # Retirement drops every in-memory handle, as a restart does; the cold
    # start learns the confinement only from what was stored.
    await _retire(child_id)
    assert not rt.is_live(child_id)
    did = await feed_with_persisted_input(
        child_id, "again", target=cc_target("sonnet"),
        turn_id="11111111-2222-4333-8444-555555555555",
    )
    assert did is not None and rt.is_live(child_id)
    assert len(launches) == 2
    _assert_confined(launches[1], child_id)
    assert launches[1]["cwd"] == launches[0]["cwd"]
    assert _stored_invocation(child_db, child_id)["confinement"] == "untrusted"
    await _retire(child_id)


@pytest.mark.asyncio
async def test_cold_start_refuses_a_confined_child_whose_reads_left_scope(
    child_db, fake_claude, launches, reviewed_dir, monkeypatch, tmp_path,
):
    """A path that has left the parent's scope fails the launch; it is never widened."""
    import connections.models as cm
    import subagents.cc_runtime as rt
    from subagents.cc_confinement import ConfinementRefused

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setattr(
        cm, "get_connection", lambda provider, db=None: {"config": {"binary": fake_claude}},
    )
    _seed_parent(child_db)
    child_id = await rt.spawn(
        "parent", "review this", binary=fake_claude, target=cc_target("sonnet"),
        broadcast=lambda _sid, _message: None,
        confinement="untrusted", read_dirs=(reviewed_dir,),
    )
    await _retire(child_id)
    shutil.rmtree(reviewed_dir)
    with pytest.raises(ConfinementRefused, match="not a directory"):
        await feed_with_persisted_input(
            child_id, "again", target=cc_target("sonnet"),
            turn_id="11111111-2222-4333-8444-555555555555",
        )
    assert len(launches) == 1 and not rt.is_live(child_id)


def test_stored_record_without_confinement_reads_unconfined():
    """(iii) a launch record written before confinement existed is ``none``."""
    old = {
        "binary": "claude", "permission_mode": "bypassPermissions",
        "session_id": "s", "resume_session_id": None, "seed_file": "seed",
        "mcp_config": "cfg.json", "working_dir": "/w", "model": "sonnet",
        "effort": None,
    }
    restored = claude_invocation_from_metadata(old)
    assert restored is not None
    assert restored.confinement == "none"
    assert claude_invocation_metadata(restored) == old


def test_a_confined_record_from_before_the_bridge_does_not_restore():
    """A confined launch used to run with no tool bridge; that record is not a launch now."""
    value = {
        "binary": "claude", "permission_mode": "dontAsk",
        "session_id": "s", "resume_session_id": None, "seed_file": "seed",
        "mcp_config": None, "working_dir": "/w", "model": "sonnet",
        "effort": None, "confinement": "untrusted", "add_dirs": ["/src"],
    }
    assert claude_invocation_from_metadata(value) is None


@pytest.mark.asyncio
async def test_review_launches_read_confinement_once_and_advertise_only_the_reads(
    child_db, fake_claude, launches, reviewed_dir, monkeypatch, tmp_path,
):
    """A cold start and a provider resume read confinement once each, before the
    destination is prepared, and build, install and budget only the confined reads."""
    import connections.models as cm
    import infra.thread_confinement as thread_confinement
    import subagents.cc_runtime as rt
    from subagents import cc_destination
    from tools.confined_reads import UNTRUSTED_READ_TOOLS

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setattr(
        cm, "get_connection", lambda provider, db=None: {"config": {"binary": fake_claude}},
    )
    _seed_parent(child_db)
    child_id = await rt.spawn(
        "parent", "review this", binary=fake_claude, target=cc_target("sonnet"),
        broadcast=lambda _sid, _message: None,
        confinement="untrusted", read_dirs=(reviewed_dir,),
    )
    reads = []
    reader = thread_confinement.read_dirs
    monkeypatch.setattr(thread_confinement, "read_dirs",
                        lambda *args, **kwargs: reads.append(args[0]) or reader(*args, **kwargs))
    fits = []
    fit = cc_destination._fit_sections
    monkeypatch.setattr(cc_destination, "_fit_sections",
                        lambda **kwargs: fits.append(fit(**kwargs)) or fits[-1])

    # Provider resume with no live handle.
    live, in_root = cc_destination._LIVE, cc_destination._require_session_in_root
    monkeypatch.setattr(cc_destination, "_LIVE", {})
    monkeypatch.setattr(cc_destination, "_require_session_in_root", lambda *_a: None)
    resumed = cc_destination.prepare_cc_destination_payload(
        child_id, target=cc_target("sonnet"), current_user_text="again",
        selection=None, provider_resume=True,
    )
    assert reads == [child_id] and resumed.confinement.mode == "untrusted"
    assert {d["function"]["name"] for d in resumed.tool_definitions} == set(UNTRUSTED_READ_TOOLS)
    assert resumed.tool_registry is not None
    # A provider resume keeps the surface Claude Code already holds, so it
    # budgets no tool tokens, confined or not.
    assert resumed.fit_section_tokens["tools"] == 0 and fits[-1][0]["tools"] == 0
    monkeypatch.setattr(cc_destination, "_LIVE", live)
    monkeypatch.setattr(cc_destination, "_require_session_in_root", in_root)

    # Cold start after retirement.
    await _retire(child_id)
    reads.clear()
    did = await feed_with_persisted_input(
        child_id, "again", target=cc_target("sonnet"),
        turn_id="11111111-2222-4333-8444-555555555555",
    )
    assert did is not None and rt.is_live(child_id)
    assert reads.count(child_id) == 1  # the parent's own launches read their own
    assert fits[-1][0]["tools"] > 0
    assert {d["function"]["name"] for d in rt._LIVE.get(child_id).tool_definitions} == set(
        UNTRUSTED_READ_TOOLS
    )
    await _retire(child_id)


@pytest.mark.asyncio
async def test_review_spawn_launches_nothing_without_its_record(
    child_db, fake_claude, launches, reviewed_dir, monkeypatch,
):
    """A review child whose launch record can't be written never starts, so no
    confined child is ever described by "no record"."""
    import registry.cc_session as cc_session
    import subagents.cc_runtime as rt

    def unwritable(*_args, **_kwargs):
        raise RuntimeError("launch record write failed")

    monkeypatch.setattr(cc_session, "persist_cc_session", unwritable)
    _seed_parent(child_db)
    with pytest.raises(RuntimeError, match="launch record write failed"):
        await rt.spawn(
            "parent", "review this", binary=fake_claude, target=cc_target("sonnet"),
            broadcast=lambda _sid, _message: None,
            confinement="untrusted", read_dirs=(reviewed_dir,),
        )
    assert launches == [] and rt._LIVE.count() == 0


@pytest.mark.asyncio
async def test_delegate_task_refuses_a_read_path_outside_the_parents_scope(
    child_db, monkeypatch, tmp_path,
):
    """(iv) each brief path passes the parent's own read checks, or nothing starts."""
    import connections.models as cm
    from subagents import cc_runtime
    from tools import delegate_task

    _seed_parent(child_db)
    monkeypatch.setattr(cm, "get_connection", lambda provider, db=None: {"config": {}})
    monkeypatch.setattr(delegate_task.delegation, "_resolve_cc_target", lambda _m: cc_target("sonnet"))
    spawn = AsyncMock(side_effect=AssertionError("no child may start"))
    monkeypatch.setattr(cc_runtime, "spawn", spawn)
    outside = tmp_path / "outside-workspace"
    outside.mkdir()

    refused = await delegate_task._spawn_claude_code_child(
        parent_thread_id="parent", task="review", title=None,
        model_id="claude-code/sonnet", confinement="untrusted", read_paths=[str(outside)],
    )
    assert refused.startswith(f"Error: read path {outside} refused: ")
    assert "outside the sandbox" in refused
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_delegate_task_refuses_another_project_before_creating_cc_child(
    child_db, monkeypatch, reviewed_dir,
):
    import connections.models as cm
    from subagents import cc_runtime
    from tools import delegate_task, project_fence

    _seed_parent(child_db)
    fence = project_fence.Fence((project_fence.FencedRoot(Path(reviewed_dir), "other"),), "parent")
    monkeypatch.setattr(project_fence, "fence_for_thread", lambda thread: fence if thread == "parent" else project_fence.Fence((), thread))
    monkeypatch.setattr(cm, "get_connection", lambda provider, db=None: {"config": {}})
    monkeypatch.setattr(delegate_task.delegation, "_resolve_cc_target", lambda _m: cc_target("sonnet"))
    spawn = AsyncMock(side_effect=AssertionError("no child may start"))
    monkeypatch.setattr(cc_runtime, "spawn", spawn)
    refused = await delegate_task._spawn_claude_code_child(
        parent_thread_id="parent", task="review", title=None,
        model_id="claude-code/sonnet", confinement="untrusted", read_paths=[reviewed_dir],
    )
    assert refused.startswith(f"Error: read path {reviewed_dir} refused: ")
    spawn.assert_not_awaited()
    assert child_db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_delegate_task_review_needs_paths_on_every_model(child_db, monkeypatch):
    from tools import delegate_task

    _seed_parent(child_db)
    unpaired = await delegate_task._spawn(
        "parent", {}, "task", {"model": "claude-code/sonnet", "confinement": "untrusted"},
    )
    assert unpaired.startswith("Error: confinement 'untrusted' needs read_paths")
    stray = await delegate_task._spawn(
        "parent", {}, "task", {"model": "claude-code/sonnet", "read_paths": ["/w"]},
    )
    assert stray.startswith("Error: confinement 'untrusted' needs read_paths")

    def eligible(model, **_scope):
        return model, None

    monkeypatch.setattr(delegate_task, "eligible_delegation_model", eligible)
    monkeypatch.setattr(delegate_task.delegation, "_is_cc_model", lambda _m: False)
    monkeypatch.setattr(delegate_task.delegation, "_is_delisted", lambda _m: False)
    monkeypatch.setattr("subagents.codex_confinement.verify_version", AsyncMock())
    monkeypatch.setattr(
        delegate_task, "create_visible_fresh_thread",
        AsyncMock(side_effect=AssertionError("no thread may be created")),
    )
    codex = await delegate_task._spawn(
        "parent", {}, "task",
        {"model": "codex/gpt-5-codex", "confinement": "untrusted", "read_paths": ["/w"]},
    )
    assert codex.startswith("Error: read path /w refused:")


def test_unconfined_launch_is_unchanged():
    """(v) a ``none`` launch builds the exact command and record it built before."""
    invocation = ClaudeInvocation(
        binary="claude", permission_mode="bypassPermissions", session_id="s",
        seed_file="seed.txt", mcp_config="cfg.json", working_dir="/w",
        model="sonnet", effort="high",
    )
    assert build_claude_cmd(invocation, stream_input=True, extra_args=["--x"]) == [
        "claude", "-p", "--input-format", "stream-json", "--replay-user-messages",
        "--output-format", "stream-json", "--verbose",
        "--include-partial-messages", "--include-hook-events",
        "--permission-mode", "bypassPermissions", "--session-id", "s",
        "--append-system-prompt-file", "seed.txt", *claude_project_discovery_args(),
        "--tools", "mcp__corsia__*", "--disable-slash-commands",
        "--mcp-config", "cfg.json", "--strict-mcp-config",
        "--model", "sonnet", "--effort", "high", "--x",
    ]
    bare = ClaudeInvocation(binary="claude", permission_mode="bypassPermissions")
    assert build_claude_cmd(bare, stream_input=False) == [
        "claude", "-p", "--output-format", "stream-json", "--verbose",
        "--include-partial-messages", "--include-hook-events",
        "--permission-mode", "bypassPermissions", *claude_project_discovery_args(),
        "--tools", "", "--disable-slash-commands",
        "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
    ]
    assert set(claude_invocation_metadata(invocation)) == _OLD_FIELDS


def test_invocation_rejects_inconsistent_confinement():
    with pytest.raises(ValueError, match="needs the CORSIA tool bridge"):
        ClaudeInvocation(binary="c", permission_mode="dontAsk", confinement="untrusted")
    with pytest.raises(ValueError):
        ClaudeInvocation(binary="c", permission_mode="p", confinement="write")
