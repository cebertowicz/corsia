
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap

import pytest

from cc_runtime_fixtures import cc_target, feed_main_with_persisted_input
from tests.conftest import _wait

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_FAKE_ERROR = textwrap.dedent(
    '''\
    #!{python}
    import sys, json
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    sys.stdin.readline()  # consume the user frame
    emit({{"type":"result","subtype":"error","is_error":True,"result":"Fatal error: something went wrong","total_cost_usd":0.0,"num_turns":0,"stop_reason":"error"}})
    '''
)


@pytest.fixture
def fake_claude_error(tmp_path):
    p = tmp_path / "fake_claude_error_result"
    p.write_text(_FAKE_ERROR.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


_FAKE_NO_ASSISTANT_SUCCESS = textwrap.dedent(
    '''\
    #!{python}
    import sys, json
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    sys.stdin.readline()  # consume the user frame
    emit({{"type":"result","subtype":"success","is_error":False,"result":"degraded-reply","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    '''
)


@pytest.fixture
def fake_claude_no_assistant_success(tmp_path):
    p = tmp_path / "fake_claude_no_assistant_success"
    p.write_text(_FAKE_NO_ASSISTANT_SUCCESS.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


_FAKE_MIXED_PARTIAL_ERROR = textwrap.dedent(
    '''\
    #!{python}
    import sys, json
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    sys.stdin.readline()  # consume the user frame
    emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"partial-output"}}]}}}})
    emit({{"type":"result","subtype":"error","is_error":True,"result":"Tool execution failed","total_cost_usd":0.0,"num_turns":1,"stop_reason":"error"}})
    '''
)


@pytest.fixture
def fake_claude_mixed_partial_error(tmp_path):
    p = tmp_path / "fake_claude_mixed_partial_error"
    p.write_text(_FAKE_MIXED_PARTIAL_ERROR.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


_FAKE_TWO_TURN_SECOND_DEGRADED = textwrap.dedent(
    '''\
    #!{python}
    import sys, json
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    sys.stdin.readline()
    emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"turn1-reply"}}]}}}})
    emit({{"type":"result","subtype":"success","is_error":False,"result":"turn1-reply","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    sys.stdin.readline()
    emit({{"type":"result","subtype":"success","is_error":False,"result":"turn2-degraded","total_cost_usd":0.0,"num_turns":2,"stop_reason":"end_turn"}})
    '''
)


@pytest.fixture
def fake_claude_two_turn_second_degraded(tmp_path):
    p = tmp_path / "fake_claude_two_turn_second_degraded"
    p.write_text(_FAKE_TWO_TURN_SECOND_DEGRADED.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)



class TestResultMarker:
    @pytest.mark.asyncio
    async def test_assistant_reply_persisted_exactly_once(self, main_db, fake_claude, tmp_path):
        import subagents.cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        await feed_main_with_persisted_input(
            "main-1", "ping", binary=fake_claude,
            target=cc_target("opus"),
            claude_dir=str(tmp_path / "empty-home"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        ok = await _wait(lambda: any(
            m.get("type") == "turn_complete" for _, m in casts))
        assert ok, "no turn_complete"

        reply = "echo:ping"
        rows = main_db.execute(
            "SELECT role, content, metadata FROM messages "
            "WHERE thread_id = 'main-1'").fetchall()

        # Per-turn frame carries the dynamic tail after the user text, which the
        # fake echoes back, so the persisted reply starts with the echoed prefix.
        matches = [r for r in rows if (r["content"] or "").startswith(reply)]
        assert len(matches) == 1, (
            f"expected 1 row with content {reply!r}, got {len(matches)}: "
            + str([(r['role'], r['content'][:60]) for r in matches]))
        assert matches[0]["role"] == "assistant", (
            f"response-text row must be 'assistant', got {matches[0]['role']!r}")

        result_rows = [
            r for r in rows
            if r["role"] == "system"
            and r["metadata"]
            and json.loads(r["metadata"]).get("type") == "result"
        ]
        assert result_rows, "no system result marker row found"
        for r in result_rows:
            assert r["content"] == "", (
                f"system result row must have empty content, got {r['content']!r}")

        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_error_result_text_preserved_in_system_marker(
            self, main_db, fake_claude_error, tmp_path):
        import subagents.cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        await feed_main_with_persisted_input(
            "main-1", "cause an error", binary=fake_claude_error,
            target=cc_target("opus"),
            claude_dir=str(tmp_path / "empty-home"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        ok = await _wait(lambda: any(
            m.get("type") == "turn_complete" for _, m in casts))
        assert ok, "no turn_complete from error turn"

        rows = main_db.execute(
            "SELECT role, content, metadata FROM messages "
            "WHERE thread_id = 'main-1'").fetchall()

        result_rows = [
            r for r in rows
            if r["role"] == "system"
            and r["metadata"]
            and json.loads(r["metadata"]).get("type") == "result"
        ]
        assert result_rows, "no system result marker row for error turn"
        r = result_rows[-1]
        meta = json.loads(r["metadata"])
        assert meta.get("is_error") is True, "is_error must be True"
        assert r["content"], (
            "error result marker must preserve the error text — "
            "got empty content (loud-failure violation)")
        assert "fatal error" in r["content"].lower(), (
            f"error text not preserved, got: {r['content']!r}")

        assert not any(
            row["role"] == "assistant" for row in rows
        ), "error turn must not produce an assistant row"

        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_degraded_path_marker_preserves_text(
            self, main_db, fake_claude_no_assistant_success, tmp_path):
        import subagents.cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        await feed_main_with_persisted_input(
            "main-1", "ping", binary=fake_claude_no_assistant_success,
            target=cc_target("opus"),
            claude_dir=str(tmp_path / "empty-home"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        ok = await _wait(lambda: any(
            m.get("type") == "turn_complete" for _, m in casts))
        assert ok, "no turn_complete from degraded-path turn"

        rows = main_db.execute(
            "SELECT role, content, metadata FROM messages "
            "WHERE thread_id = 'main-1'").fetchall()

        assert not any(
            row["role"] == "assistant" for row in rows
        ), "degraded-path turn must not produce an assistant row"

        result_rows = [
            r for r in rows
            if r["role"] == "system"
            and r["metadata"]
            and json.loads(r["metadata"]).get("type") == "result"
        ]
        assert result_rows, "no system result marker row found"
        r = result_rows[-1]
        meta = json.loads(r["metadata"])
        assert meta.get("is_error") is False, "degraded success must have is_error=False"
        assert r["content"] == "degraded-reply", (
            f"degraded-path marker must keep result text; got {r['content']!r}")

        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_mixed_partial_then_error_marker_keeps_error_text(
            self, main_db, fake_claude_mixed_partial_error, tmp_path):
        import subagents.cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        await feed_main_with_persisted_input(
            "main-1", "do something", binary=fake_claude_mixed_partial_error,
            target=cc_target("opus"),
            claude_dir=str(tmp_path / "empty-home"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        ok = await _wait(lambda: any(
            m.get("type") == "turn_complete" for _, m in casts))
        assert ok, "no turn_complete from mixed-path turn"

        rows = main_db.execute(
            "SELECT role, content, metadata FROM messages "
            "WHERE thread_id = 'main-1'").fetchall()

        assistant_rows = [r for r in rows if r["role"] == "assistant"]
        assert assistant_rows, "mixed-path turn must produce an assistant row"
        assert any(r["content"] == "partial-output" for r in assistant_rows), (
            "assistant row must carry the partial output text")

        result_rows = [
            r for r in rows
            if r["role"] == "system"
            and r["metadata"]
            and json.loads(r["metadata"]).get("type") == "result"
        ]
        assert result_rows, "no system result marker row found"
        r = result_rows[-1]
        meta = json.loads(r["metadata"])
        assert meta.get("is_error") is True, "mixed-path result must have is_error=True"
        assert r["content"], (
            "mixed-path error marker must keep the error text — "
            "got empty content (loud-failure violation)")
        assert "tool execution failed" in r["content"].lower(), (
            f"error text not preserved, got: {r['content']!r}")

        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_two_turn_normal_then_degraded(
            self, main_db, fake_claude_two_turn_second_degraded, tmp_path):
        import subagents.cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        kw = dict(target=cc_target("opus"),
                  binary=fake_claude_two_turn_second_degraded,
                  claude_dir=str(tmp_path / "empty-home"),
                  broadcast=lambda sid, m: casts.append((sid, m)))

        await feed_main_with_persisted_input("main-1", "first", **kw)
        ok = await _wait(lambda: sum(
            1 for _, m in casts if m.get("type") == "turn_complete") >= 1)
        assert ok, "no turn_complete after turn 1"

        await feed_main_with_persisted_input(
            "main-1", "second", target=cc_target("opus"), claude_dir=kw["claude_dir"]
        )
        ok = await _wait(lambda: sum(
            1 for _, m in casts if m.get("type") == "turn_complete") >= 2)
        assert ok, "no turn_complete after turn 2"

        rows = main_db.execute(
            "SELECT role, content, metadata, created_at FROM messages "
            "WHERE thread_id = 'main-1' ORDER BY created_at").fetchall()

        result_rows = [
            r for r in rows
            if r["role"] == "system"
            and r["metadata"]
            and json.loads(r["metadata"]).get("type") == "result"
        ]
        assert len(result_rows) == 2, (
            f"expected 2 result markers, got {len(result_rows)}")

        r1 = result_rows[0]
        assert r1["content"] == "", (
            f"turn 1 marker must be blank, got {r1['content']!r}")

        r2 = result_rows[1]
        assert r2["content"] == "turn2-degraded", (
            f"turn 2 degraded marker must keep result text; got {r2['content']!r}")

        await rt.retire("main-1")

    @pytest.mark.asyncio
    async def test_dropped_line_flag_preserves_marker(
            self, main_db, fake_claude_two_turn_second_degraded, tmp_path):
        import subagents.cc_runtime as rt

        casts: list[tuple[str, dict]] = []
        kw = dict(target=cc_target("opus"),
                  binary=fake_claude_two_turn_second_degraded,
                  claude_dir=str(tmp_path / "empty-home"),
                  broadcast=lambda sid, m: casts.append((sid, m)))

        await feed_main_with_persisted_input("main-1", "first", **kw)
        ok = await _wait(lambda: sum(
            1 for _, m in casts if m.get("type") == "turn_complete") >= 1)
        assert ok, "no turn_complete after turn 1"

        live = rt._LIVE.get("main-1")
        assert live is not None, "session not live after turn 1"
        live.turn_has_assistant_row = True   # stale from a prior turn
        live.dropped_line_this_turn = True   # dropped line this turn
        ok = await _wait(lambda: (
            live.turn_terminal is not None
            and live.turn_terminal.is_set()
        ))
        assert ok, "turn 1 terminal post-processing did not drain"

        await feed_main_with_persisted_input(
            "main-1", "second", target=cc_target("opus"), claude_dir=kw["claude_dir"]
        )
        ok = await _wait(lambda: sum(
            1 for _, m in casts if m.get("type") == "turn_complete") >= 2)
        assert ok, "no turn_complete after turn 2"

        rows = main_db.execute(
            "SELECT role, content, metadata, created_at FROM messages "
            "WHERE thread_id = 'main-1' ORDER BY created_at").fetchall()

        result_rows = [
            r for r in rows
            if r["role"] == "system"
            and r["metadata"]
            and json.loads(r["metadata"]).get("type") == "result"
        ]
        assert len(result_rows) == 2, f"expected 2 result markers, got {len(result_rows)}"

        r2 = result_rows[1]
        assert r2["content"] == "turn2-degraded", (
            f"dropped_line_this_turn must prevent blanking even with stale "
            f"turn_has_assistant_row=True; got {r2['content']!r}")

        await rt.retire("main-1")
