"""A ``untrusted`` launch has no native tool: it reads through the CORSIA bridge.

The command line is checked directly, and a stand-in ``claude`` declares the
init tools and makes the one call its environment names, so each stream case
runs the real spawn, stream and event pump. The stand-in reports init once its
first input arrives, so a boundary verdict never lands while the spawn's first
write is still in flight.
"""

from __future__ import annotations

import json
import shutil
import stat
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from cc_runtime_fixtures import _seed_parent, _wait, cc_target, child_db  # noqa: F401
from subagents.cc_invocation import (
    ClaudeInvocation,
    build_claude_cmd,
    confined_claude_tool_names,
    permission_mode_for,
)
from tools.confined_reads import UNTRUSTED_READ_TOOLS

#: Claude Code's own tools; none of them may appear on a confined launch.
_NATIVE_TOOL_NAMES = ("Read", "Grep", "Glob", "Bash", "Write", "Edit", "Task", "WebFetch")

_STANDIN = textwrap.dedent(
    '''\
    #!{python}
    import json, os, sys
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    tools = [t for t in os.environ.get("STANDIN_TOOLS", "").split(",") if t]
    call = os.environ.get("STANDIN_CALL", "")
    init = {{"type":"system","subtype":"init","session_id":"fake","model":"m",
            "tools":tools,"mcp_servers":[],"cwd":os.getcwd()}}
    for line in sys.stdin:
        if not line.strip():
            continue
        if init:
            emit(init); init = None
        if call:
            emit({{"type":"assistant","message":{{"id":"msg-call","content":[
                {{"type":"tool_use","id":"toolu-native","name":call,
                  "input":{{"file_path":"notes.md"}}}}]}}}})
            emit({{"type":"user","message":{{"role":"user","content":[
                {{"type":"tool_result","tool_use_id":"toolu-native","content":"file body"}}]}}}})
        emit({{"type":"assistant","message":{{"id":"msg-done","content":[
            {{"type":"text","text":"review done"}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":"review done",
              "total_cost_usd":0.0,"num_turns":2,"stop_reason":"end_turn"}})
    '''
)


@pytest.fixture
def standin(tmp_path):
    path = tmp_path / "standin_claude"
    path.write_text(_STANDIN.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def reviewed_dir():
    from infra.config import WORKSPACE

    path = Path(WORKSPACE) / f"review-src-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    yield str(path.resolve())
    shutil.rmtree(path, ignore_errors=True)


def _confined_invocation(**overrides) -> ClaudeInvocation:
    fields = {
        "binary": "claude",
        "permission_mode": permission_mode_for("untrusted"),
        "session_id": "sid",
        "mcp_config": "bridge.json",
        "working_dir": "/confined-cwd",
        "confinement": "untrusted",
        "add_dirs": ("/reviewed",),
    } | overrides
    return ClaudeInvocation(**fields)


def test_confined_argv_offers_only_the_bridged_read_tools():
    """(a) ``--tools`` is exactly the four read tools; no native tool is anywhere."""
    argv = build_claude_cmd(_confined_invocation(), stream_input=False)

    assert confined_claude_tool_names() == [
        f"mcp__corsia__{name}" for name in sorted(UNTRUSTED_READ_TOOLS)
    ]
    assert argv[argv.index("--tools") + 1] == ",".join(confined_claude_tool_names())
    assert not any(name in argv for name in _NATIVE_TOOL_NAMES)
    assert "--add-dir" not in argv


def test_confined_argv_carries_the_bridge_and_none_without_it():
    """(b) a confined launch runs the bridge; one with no bridge never builds."""
    argv = build_claude_cmd(_confined_invocation(), stream_input=False)

    assert argv[argv.index("--mcp-config") + 1] == "bridge.json"
    assert "--strict-mcp-config" in argv
    with pytest.raises(ValueError, match="needs the CORSIA tool bridge"):
        _confined_invocation(mcp_config=None)


def test_a_confined_launch_advertises_only_the_read_tools(isolated_db):
    """(c) what the confined launch installs and budgets is the four confined reads.

    The registry is the ordinary one, built on the test session's own workspace:
    the confinement is the projection, not a different registry, so nothing here
    repoints the workspace at a temporary directory. (Doing that before this
    module's first import of ``tools.sandbox_config`` leaves the fake workspace
    as a sandbox root for every later test in the run.)
    """
    from subagents.cc_confinement import launch_tools

    registry, definitions = launch_tools(None, True)

    assert registry is not None
    assert {d["function"]["name"] for d in definitions} == set(UNTRUSTED_READ_TOOLS)


@pytest.mark.asyncio
async def test_confined_launch_is_never_forked_warm(monkeypatch, tmp_path):
    """A checkpoint fork never resumes a confined session; the refusal is named."""
    from types import SimpleNamespace

    from loguru import logger

    from core import fork_cc
    from history.types import HistoryBoundary
    from subagents.cc_invocation import ClaudeInvocation

    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user"}\n')
    monkeypatch.setattr(fork_cc, "_transcript_path", lambda _sid, _root: str(transcript))
    invocation = ClaudeInvocation(
        binary="claude", permission_mode="dontAsk", session_id="sid",
        mcp_config="bridge.json", working_dir=str(tmp_path),
        confinement="untrusted", add_dirs=(str(tmp_path),),
    )
    live = SimpleNamespace(
        session=SimpleNamespace(invocation=lambda: invocation), claude_dir=str(tmp_path),
    )
    reasons: list[str] = []
    sink = logger.add(
        lambda message: reasons.append(str(message).rsplit(": ", 1)[-1].strip()),
        level="INFO", format="{message}",
        filter=lambda record: "warm capture refused" in record["message"],
    )
    try:
        source = await fork_cc.capture_warm_source(
            "child", HistoryBoundary("child", 0, 1, "m1"),
            target=SimpleNamespace(harness="claude-code"), binary="claude", live=live,
        )
    finally:
        logger.remove(sink)
    assert source is None and reasons == ["confined_launch"]


async def _run(child_db, standin, monkeypatch, tmp_path, *, tools, call, confined):
    import connections.models as cm
    import subagents.cc_runtime as rt

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setenv("STANDIN_TOOLS", ",".join(tools))
    monkeypatch.setenv("STANDIN_CALL", call)
    monkeypatch.setattr(
        cm, "get_connection", lambda provider, db=None: {"config": {"binary": standin}},
    )
    _seed_parent(child_db)
    confinement = {"confinement": "untrusted", "add_dirs": (confined,)} if confined else {}
    child_id = await rt.spawn(
        "parent", "review this", binary=standin, target=cc_target("sonnet"),
        broadcast=lambda _sid, _message: None, **confinement,
    )
    assert await _wait(lambda: _ended(child_db, child_id))
    return child_id


def _rows(db, child_id: str) -> list[tuple[str, str, dict]]:
    return [
        (row["role"], row["content"] or "", json.loads(row["metadata"] or "{}"))
        for row in db.execute(
            "SELECT role, content, metadata FROM messages WHERE thread_id=? ORDER BY seq",
            (child_id,),
        ).fetchall()
    ]


def _violations(db, child_id: str) -> list[dict]:
    return [
        meta for role, _content, meta in _rows(db, child_id)
        if role == "system" and meta.get("type") == "tool_boundary_violation"
    ]


def _ended(db, child_id: str) -> bool:
    return bool(_violations(db, child_id)) or any(
        role == "assistant" and content == "review done"
        for role, content, _meta in _rows(db, child_id)
    )


@pytest.mark.asyncio
async def test_confined_launch_refuses_a_native_tool_it_declares(
    child_db, standin, reviewed_dir, monkeypatch, tmp_path,
):
    """A confined launch that declares one of Claude Code's own tools crosses the boundary."""
    child_id = await _run(
        child_db, standin, monkeypatch, tmp_path,
        tools=(*confined_claude_tool_names(), "Read"), call="", confined=reviewed_dir,
    )
    [violation] = _violations(child_db, child_id)
    assert violation["capability"] == "init" and violation["native_tools"] == ["Read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("confined", [True, False], ids=["confined", "unconfined"])
async def test_a_native_tool_call_is_never_run(
    child_db, standin, reviewed_dir, monkeypatch, tmp_path, confined,
):
    """(e) a native call is answered as a tool CORSIA does not serve, on any launch."""
    from infra.delivery_states import UNDISPATCHED_TOOL_REASON

    child_id = await _run(
        child_db, standin, monkeypatch, tmp_path,
        tools=confined_claude_tool_names(), call="Read",
        confined=reviewed_dir if confined else None,
    )
    settled = [(content, meta) for role, content, meta in _rows(child_db, child_id) if role == "tool"]
    assert _violations(child_db, child_id) == []
    assert len(settled) == 1
    content, meta = settled[0]
    assert "Read` is not a tool CORSIA serves" in content
    assert meta.get("is_error") and meta.get("synthetic_reason") == UNDISPATCHED_TOOL_REASON
    # The Step that declared the call is persisted; CORSIA answered it and the
    # turn finished, so nothing was ever dispatched to the bridge.
    assistant = [content for role, content, _ in _rows(child_db, child_id) if role == "assistant"]
    assert assistant[-1] == "review done"
