"""A ``review`` launch reads with Claude Code's own tools, and only those.

A stand-in ``claude`` declares the init tools and makes the one native call its
environment names, so each case runs the real spawn, stream and event pump. It
reports init once its first input arrives, so a boundary verdict never lands
while the spawn's first write is still in flight.
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
from subagents.cc_invocation import REVIEW_NATIVE_TOOLS, claude_review_profile_args

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
    confinement = {"confinement": "review", "add_dirs": (confined,)} if confined else {}
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


def test_review_admits_exactly_the_tools_its_launch_allows():
    """One list builds ``--tools`` and the boundary's admission."""
    profile = claude_review_profile_args()
    assert profile[profile.index("--tools") + 1] == ",".join(REVIEW_NATIVE_TOOLS)
    assert REVIEW_NATIVE_TOOLS == ("Read", "Grep", "Glob")


@pytest.mark.asyncio
async def test_review_launch_runs_a_native_read_inside_the_provider(
    child_db, standin, reviewed_dir, monkeypatch, tmp_path,
):
    child_id = await _run(
        child_db, standin, monkeypatch, tmp_path,
        tools=REVIEW_NATIVE_TOOLS, call="Read", confined=reviewed_dir,
    )
    rows = _rows(child_db, child_id)
    assert _violations(child_db, child_id) == []
    assert [content for role, content, _ in rows if role == "assistant"] == ["review done"]
    # The read is the provider's own transcript content, never a CORSIA tool row.
    assert [role for role, _content, _meta in rows if role == "tool"] == []
    assert all(not meta.get("tool_calls") for role, _c, meta in rows if role == "assistant")


@pytest.mark.asyncio
async def test_unconfined_launch_still_refuses_a_native_tool(
    child_db, standin, monkeypatch, tmp_path,
):
    child_id = await _run(
        child_db, standin, monkeypatch, tmp_path,
        tools=("Read",), call="Read", confined=None,
    )
    [violation] = _violations(child_db, child_id)
    assert violation["capability"] == "init" and violation["native_tools"] == ["Read"]


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
        working_dir=str(tmp_path), confinement="review", add_dirs=(str(tmp_path),),
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


@pytest.mark.asyncio
@pytest.mark.parametrize(("tools", "call", "capability", "named"), [
    ((*REVIEW_NATIVE_TOOLS, "Write"), "", "init", ["Write"]),
    (REVIEW_NATIVE_TOOLS, "Bash", "native_tool", ["Bash"]),
    (REVIEW_NATIVE_TOOLS, "mcp__corsia__read_file", "native_tool", ["mcp__corsia__read_file"]),
])
async def test_review_launch_refuses_every_other_tool(
    child_db, standin, reviewed_dir, monkeypatch, tmp_path, tools, call, capability, named,
):
    child_id = await _run(
        child_db, standin, monkeypatch, tmp_path,
        tools=tools, call=call, confined=reviewed_dir,
    )
    [violation] = _violations(child_db, child_id)
    assert violation["capability"] == capability and violation["native_tools"] == named
    assert [role for role, _content, _meta in _rows(child_db, child_id) if role == "tool"] == []
