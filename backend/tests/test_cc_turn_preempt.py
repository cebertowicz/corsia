"""Claude Code keeps one live Turn and never preempts it for a normal send.

The invariant under test: **≤1 live turn per thread; context is only built from
committed history; normal input stays in the durable FIFO, while only an
explicit Stop may interrupt the exact provider turn.**

The low-level interruption tests remain here because a deliberate Stop still
must settle tool work and terminal state safely.  The normal-send tests prove
that those primitives are never entered as a convenience shortcut.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_runtime_fixtures import (  # noqa: F401
    _done_count,
    _ready_count,
    _seed_parent,
    _wait,
    cc_target,
    child_db,
    persist_bridge_tool_result,
)


def _admit_active_turn(db, thread_id: str, turn_id: str):
    from core.turn_core import BOOT_EPOCH
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    db.execute(
        "UPDATE threads SET model='claude-code/sonnet' WHERE id=?",
        (thread_id,),
    )
    db.commit()
    admission = admit_input(
        thread_id,
        role="user",
        content="first",
        intent="apply_now",
        message_id=f"{turn_id}:input",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    attempt_id = f"{turn_id}:write"
    begin_batch_write(
        admission.batch, attempt_id=attempt_id, started_at="t", db=db,
    )
    settle_batch_write(attempt_id, outcome="accepted", db=db)
    return admission.batch.identity


async def _preempt_for_test(cc_preempt, thread_id, live, child_db, casts):
    return await cc_preempt.preempt_active_turn(
        thread_id, live,
        target=cc_target("sonnet"), effort=None,
        claude_dir=None, binary=None,
        broadcast=None, is_subagent=False, text="next",
        bcast=lambda sid, message: casts.append((sid, message)), db=child_db,
        held_lock=cc_preempt._LIVE.lock(thread_id),
        preempt_authorized=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_stale_stop_cannot_close_replacement_behind_transition_lock():
    from core.turn_core import TurnIdentity
    from subagents import cc_runtime as rt

    thread_id = "cc-stale-stop-timeout"
    replacement_session = type("Session", (), {})()
    replacement_session.close = AsyncMock()
    replacement_session.terminate = AsyncMock()
    replacement = rt._Live(
        session=replacement_session,
        seed_file="/dev/null",
        parent_thread_id=None,
        name="replacement",
        corsia_turn=TurnIdentity("turn-B", 2),
    )
    rt._LIVE.put(thread_id, replacement)
    lock = rt._LIVE.lock(thread_id)
    await lock.acquire()
    try:
        assert await rt.stop(thread_id, expected_turn_id="turn-A") is False
        assert rt._LIVE.get(thread_id) is replacement
        assert replacement.checkpoint_wait_scope.stop_requested is False
        replacement_session.close.assert_not_awaited()
        replacement_session.terminate.assert_not_awaited()
    finally:
        lock.release()
        rt._LIVE.reset()

# ── Fakes ─────────────────────────────────────────────────────────────────────
# Both fakes log every parsed stdin frame (tagged with their pid) to a shared
# frames log so tests can assert delivery ORDER and which process got what.
# A user frame whose content contains "HANG-NOW" opens a turn (partial
# assistant text, no result) and leaves it open; any other user frame echoes
# and terminates.  Spawned agent input now carries a provenance envelope, so
# the task text is no longer necessarily the leading line of the frame.

_FAKE_INTERRUPTIBLE = textwrap.dedent(
    '''\
    #!{python}
    import os, sys, json
    LOG = open("{framelog}", "a", buffering=1)
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        LOG.write(json.dumps({{"pid": os.getpid(), "frame": msg}}) + "\\n")
        t = msg.get("type")
        if t == "user":
            c = msg.get("message", {{}}).get("content", "")
            if "HANG-NOW" in c:
                emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"partial-one"}}]}}}})
                # no result — the turn stays open until interrupted
            else:
                emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo2:" + c}}]}}}})
                emit({{"type":"result","subtype":"success","is_error":False,"result":"done","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
        elif t == "control_request" and (msg.get("request") or {{}}).get("subtype") == "interrupt":
            emit({{"type":"control_response","response":{{"request_id":msg.get("request_id"),"subtype":"success"}}}})
            emit({{"type":"result","subtype":"error_during_execution","is_error":False,"result":"","total_cost_usd":0.0,"num_turns":1,"stop_reason":None}})
    '''
)

# Ignores control_request entirely — forces the hard-preempt (kill + resume).
_FAKE_DEAF = textwrap.dedent(
    '''\
    #!{python}
    import os, sys, json
    LOG = open("{framelog}", "a", buffering=1)
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        LOG.write(json.dumps({{"pid": os.getpid(), "frame": msg}}) + "\\n")
        t = msg.get("type")
        if t == "user":
            c = msg.get("message", {{}}).get("content", "")
            if "HANG-NOW" in c:
                emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"partial-one"}}]}}}})
            else:
                emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo2:" + c}}]}}}})
                emit({{"type":"result","subtype":"success","is_error":False,"result":"done","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    '''
)


# Completes NATURALLY (stop_reason end_turn) when the interrupt arrives —
# models the race where the turn finishes on its own in the preempt window.
_FAKE_NATURAL_RACE = textwrap.dedent(
    '''\
    #!{python}
    import os, sys, json
    LOG = open("{framelog}", "a", buffering=1)
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        LOG.write(json.dumps({{"pid": os.getpid(), "frame": msg}}) + "\\n")
        t = msg.get("type")
        if t == "user":
            c = msg.get("message", {{}}).get("content", "")
            if "HANG-NOW" in c:
                emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"partial-one"}}]}}}})
            else:
                emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo2:" + c}}]}}}})
                emit({{"type":"result","subtype":"success","is_error":False,"result":"done","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
        elif t == "control_request" and (msg.get("request") or {{}}).get("subtype") == "interrupt":
            emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"finished-naturally"}}]}}}})
            emit({{"type":"result","subtype":"success","is_error":False,"result":"finished","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    '''
)


def _write_fake(tmp_path, name: str, template: str) -> tuple[str, str]:
    framelog = tmp_path / f"{name}.frames.jsonl"
    framelog.touch()
    p = tmp_path / name
    p.write_text(template.format(python=sys.executable, framelog=framelog))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p), str(framelog)


@pytest.fixture
def fake_claude_interruptible(tmp_path):
    return _write_fake(tmp_path, "fake_claude_interruptible", _FAKE_INTERRUPTIBLE)


@pytest.fixture
def fake_claude_natural_race(tmp_path):
    return _write_fake(tmp_path, "fake_claude_natural_race", _FAKE_NATURAL_RACE)


@pytest.fixture
def fake_claude_deaf(tmp_path):
    return _write_fake(tmp_path, "fake_claude_deaf", _FAKE_DEAF)


def _frames(framelog: str) -> list[dict]:
    with open(framelog) as f:
        return [json.loads(line) for line in f if line.strip()]


def _turn_markers(db, thread_id: str) -> list[dict]:
    """System result markers for a session in commit (rowid) order."""
    rows = db.execute(
        "SELECT rowid, role, content, metadata FROM messages "
        "WHERE thread_id = ? AND role = 'system' ORDER BY rowid",
        (thread_id,),
    ).fetchall()
    out = []
    for r in rows:
        meta = json.loads(r["metadata"] or "{}")
        if meta.get("type") == "result":
            out.append({"rowid": r["rowid"], "meta": meta})
    return out


def _user_rowid(db, thread_id: str, needle: str) -> int | None:
    row = db.execute(
        "SELECT rowid FROM messages WHERE thread_id = ? AND role = 'user' "
        "AND content LIKE ? ORDER BY rowid LIMIT 1",
        (thread_id, f"%{needle}%"),
    ).fetchone()
    return row["rowid"] if row else None


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestGracefulPreempt:
    @pytest.mark.asyncio
    async def test_busy_input_does_not_preempt_or_persist_successor(
        self, child_db, monkeypatch,
    ):
        from core.turn_core import TurnIdentity
        from subagents import cc_runtime as rt
        from subagents import cc_feed, cc_feed_transition, cc_preempt

        thread_id = "cc-delayed-persist"
        _seed_parent(child_db, sid=thread_id)
        session = type("Session", (), {"returncode": None})()
        order: list[str] = []
        live = rt._Live(
            session=session, parent_thread_id=None, name="cc",
            corsia_turn=TurnIdentity("predecessor", 1), turn_active=True,
            model="sonnet",
        )

        async def _persist_once():
            order.append("persist-successor")

        async def _accept(*_args, **_kwargs):
            order.append("accept-successor")
            return TurnIdentity("successor", 2)

        frame = type("Frame", (), {})()
        preempt = AsyncMock()
        monkeypatch.setattr(cc_preempt, "preempt_active_turn", preempt)
        monkeypatch.setattr(cc_feed_transition, "_proc_is_alive", lambda _session: True)
        monkeypatch.setattr(cc_feed, "build_cc_user_frame", lambda **_kwargs: frame)
        monkeypatch.setattr(cc_feed, "accept_reconstructed_input", _accept)
        rt._LIVE.put(thread_id, live)
        try:
            with pytest.raises(rt.CCLiveTurnBusy):
                await rt.ensure_live_then_feed(
                    thread_id,
                    "replacement",
                    target=cc_target("sonnet"),
                    turn_id="successor",
                    is_subagent=False,
                    record=False,
                    persist_input_once=_persist_once,
                    db=None,
                )
        finally:
            rt._LIVE.reset()

        preempt.assert_not_awaited()
        assert order == []
        assert live.turn_active is True

    @pytest.mark.asyncio
    async def test_active_tool_reports_boundary_without_interrupt(self):
        from subagents import cc_runtime as rt
        from subagents import cc_preempt

        session = type("Session", (), {})()
        live = None

        async def interrupt():
            assert live is not None
            live.turn_active = False
            assert live.turn_terminal is not None
            live.turn_terminal.set()
            return True

        session.interrupt = AsyncMock(side_effect=interrupt)
        session.terminate = AsyncMock()
        live = rt._Live(
            session=session, parent_thread_id=None, name="cc",
            turn_active=True,
        )
        live.tool_names["tool-1"] = "Read"
        live.tool_step_ids["tool-1"] = "step-1"
        live.tools_settled.clear()
        result = await cc_preempt.preempt_active_turn(
            "cc-tool-boundary", live,
            target=cc_target("sonnet"), effort=None,
            claude_dir=None, binary=None,
            broadcast=None, is_subagent=False, text="next",
            bcast=lambda _sid, _message: None,
            held_lock=cc_preempt._LIVE.lock("cc-tool-boundary"),
        )

        assert result == cc_preempt.TOOL_BOUNDARY_ACTIVE
        session.interrupt.assert_not_awaited()
        session.terminate.assert_not_awaited()
        assert live.turn_active is True
        assert live.interrupt_requested is False
        assert live.turn_terminal is None
        assert live.tool_step_ids == {"tool-1": "step-1"}

    @pytest.mark.asyncio
    async def test_real_tool_result_commits_before_interrupt_control(
        self, child_db,
    ):
        from subagents import cc_runtime as rt
        from subagents import cc_preempt
        from subagents.cc_runtime_events import _make_on_event

        thread_id = "cc-safe-boundary"
        _seed_parent(child_db, sid=thread_id)
        interrupt_observations: list[int] = []
        on_event = None

        class Session:
            pid = 1

            async def interrupt(self):
                durable_results = child_db.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE thread_id = ? "
                    "AND role = 'tool'",
                    (thread_id,),
                ).fetchone()["n"]
                interrupt_observations.append(durable_results)
                asyncio.create_task(on_event({
                    "kind": "result", "result": "", "is_error": False,
                }))
                return True

            async def terminate(self):
                raise AssertionError("graceful safe-boundary preempt must not kill")

        identity = _admit_active_turn(
            child_db, thread_id, "turn-safe-boundary",
        )
        live = rt._Live(
            session=Session(), parent_thread_id=None, name="cc",
            corsia_turn=identity, turn_active=True,
        )
        casts: list[tuple[str, dict]] = []
        on_event = _make_on_event(
            live, thread_id,
            lambda sid, message: casts.append((sid, message)), child_db,
        )
        await on_event({
            "kind": "assistant_step", "text": "checking", "reasoning": "",
            "tool_uses": [{
                "id": "read-1", "name": "mcp__corsia__read_file",
                "input": {"file_path": "/tmp/a"},
            }],
        })

        first = await _preempt_for_test(
            cc_preempt, thread_id, live, child_db, casts
        )
        assert first == cc_preempt.TOOL_BOUNDARY_ACTIVE
        assert interrupt_observations == []

        persist_bridge_tool_result(
            child_db, live, thread_id=thread_id,
            provider_tool_call_id="read-1", content="contents",
        )
        await on_event({
            "kind": "tool_result", "tool_use_id": "read-1",
            "content": "contents", "is_error": False,
        })
        assert await _preempt_for_test(
            cc_preempt, thread_id, live, child_db, casts
        ) is live

        assert interrupt_observations == [1]
        rows = [dict(row) for row in child_db.execute(
            "SELECT role, metadata FROM messages WHERE thread_id = ? ORDER BY rowid",
            (thread_id,),
        ).fetchall()]
        assert json.loads(rows[0]["metadata"])["event_type"] == "context.digest"
        assert [row["role"] for row in rows[1:]] == [
            "user", "assistant", "tool", "system",
        ]
        assert json.loads(rows[3]["metadata"])["assistant_step_id"]
        assert json.loads(rows[4]["metadata"])["stop_reason"] == "interrupted"

    @pytest.mark.asyncio
    async def test_terminal_missing_result_is_surfaced_once_and_quarantines_context(
        self, child_db,
    ):
        from subagents import cc_runtime as rt
        from subagents.cc_runtime_events import _make_on_event

        thread_id = "cc-missing-result"
        _seed_parent(child_db, sid=thread_id)
        identity = _admit_active_turn(
            child_db, thread_id, "turn-missing-result",
        )
        live = rt._Live(
            session=None, parent_thread_id=None, name="cc",
            corsia_turn=identity, turn_active=True,
        )
        on_event = _make_on_event(
            live, thread_id, lambda _sid, _message: None, child_db,
        )
        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "lost-1",
                "name": "mcp__corsia__read_file",
                "input": {},
            }],
        })
        await on_event({
            "kind": "result", "result": "provider ended", "is_error": True,
        })

        tool_rows = child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id = ? AND role = 'tool'",
            (thread_id,),
        ).fetchall()
        assert len(tool_rows) == 1
        metadata = json.loads(tool_rows[0]["metadata"])
        assert metadata["is_error"] is True
        assert metadata["synthetic_tool_result"] is True
        assert metadata["synthetic_reason"] == "provider_missing_result"
        assert live.tool_step_ids == {}
        assert live.tools_settled.is_set()
        assert live.provider_context_untrusted is True

    @pytest.mark.asyncio
    async def test_late_real_result_supersedes_provider_missing_projection(
        self, child_db,
    ):
        from core.message_display import llm_to_display
        from subagents import cc_runtime as rt
        from subagents.cc_runtime_events import _make_on_event

        thread_id = "cc-late-real-result"
        _seed_parent(child_db, sid=thread_id)
        identity = _admit_active_turn(
            child_db, thread_id, "turn-late-real-result",
        )
        live = rt._Live(
            session=None, parent_thread_id=None, name="cc",
            corsia_turn=identity, turn_active=True,
        )
        on_event = _make_on_event(
            live, thread_id, lambda _sid, _message: None, child_db,
        )
        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "late-1",
                "name": "mcp__corsia__read_file",
                "input": {},
            }],
        })
        # The bridge persisted the canonical result at execution time; only
        # the provider echo was lost to the abnormal terminal below. The late
        # echo carries the invocation marker the bridge appended, which is what
        # disambiguates it from the synthetic missing-result row.
        delivery = persist_bridge_tool_result(
            child_db, live, thread_id=thread_id,
            provider_tool_call_id="late-1", content="real contents",
        )
        await on_event({
            "kind": "result", "result": "provider ended", "is_error": True,
        })
        await on_event({
            "kind": "tool_result", "tool_use_id": "late-1",
            "content": delivery.render_for_model(
                thread_id=thread_id, budget_tokens=None,
                include_invocation_reference=True,
            ),
            "is_error": False,
        })

        rows = [dict(row) for row in child_db.execute(
            "SELECT *, rowid FROM messages WHERE thread_id = ? "
            "AND role IN ('assistant', 'tool') ORDER BY created_at, rowid",
            (thread_id,),
        ).fetchall()]
        # ONE tool row, not two. The bridge persisted the canonical result
        # before the abnormal terminal, so settlement holds the bytes it needs
        # and no longer authors "Claude Code did not provide a result" beside
        # them. The pair this used to assert was the contradiction that made a
        # later echo unbindable (2026-08-15); not writing it is the fix.
        assert sum(row["role"] == "tool" for row in rows) == 1
        projected = llm_to_display(rows)
        call = json.loads(projected[0]["metadata"])["tool_calls"][0]
        assert call["result"] == "real contents"
        assert call.get("synthetic_tool_result") is not True

    @pytest.mark.asyncio
    async def test_abnormal_exit_settles_active_tool_once_before_terminal(
        self, child_db,
    ):
        from subagents import cc_runtime as rt
        from subagents.cc_runtime_events import _make_on_event

        thread_id = "cc-exit-missing-result"
        _seed_parent(child_db, sid=thread_id)
        identity = _admit_active_turn(
            child_db, thread_id, "turn-exit-missing-result",
        )
        live = rt._Live(
            session=None, parent_thread_id=None, name="cc",
            corsia_turn=identity, turn_active=True,
        )
        casts: list[tuple[str, dict]] = []
        bcast = lambda sid, message: casts.append((sid, message))
        on_event = _make_on_event(live, thread_id, bcast, child_db)
        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "lost-on-exit",
                "name": "mcp__corsia__read_file",
                "input": {},
            }],
        })

        first = await rt._finish_process_exit(
            thread_id, live, 1, "provider exited", bcast, child_db,
        )
        duplicate = await rt._finish_process_exit(
            thread_id, live, 1, "provider exited again", bcast, child_db,
        )

        assert first.value == "applied"
        assert duplicate.value == "already_same_outcome"
        rows = child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id = ? AND role = 'tool'",
            (thread_id,),
        ).fetchall()
        assert len(rows) == 1
        metadata = json.loads(rows[0]["metadata"])
        assert metadata == {
            "tool_call_id": "lost-on-exit",
            "name": "mcp__corsia__read_file",
            "assistant_step_id": live.last_assistant_message_id,
            "is_error": True,
            "synthetic_tool_result": True,
            "synthetic_reason": "provider_missing_result",
            "tool_invocation_id": (
                "synthetic:provider_missing_result:lost-on-exit"
            ),
            "tool_result_binding_state": "bound",
            "arguments": {},
            "tool_result_sha256": metadata["tool_result_sha256"],
            "tool_result_byte_count": metadata["tool_result_byte_count"],
            "provider_tool_call_id": "lost-on-exit",
            "turn_id": identity.turn_id,
        }
        assert live.tool_step_ids == {}
        assert live.tools_settled.is_set()
        assert live.provider_context_untrusted is True

    @pytest.mark.asyncio
    async def test_boundary_exit_redacts_stderr_when_marker_insert_failed(
        self,
        child_db,
    ):
        from subagents import cc_runtime as rt

        thread_id = "cc-boundary-marker-insert-failed"
        _seed_parent(child_db, sid=thread_id)
        identity = _admit_active_turn(
            child_db, thread_id, "turn-boundary-marker-insert-failed",
        )
        live = rt._Live(
            session=None,
            parent_thread_id=None,
            name="cc",
            corsia_turn=identity,
            turn_active=True,
            tool_boundary_violation_recorded=True,
            tool_boundary_marker_id=None,
        )
        secret = "secret /Users/example/.ssh/id_ed25519 output"

        result = await rt._finish_process_exit(
            thread_id,
            live,
            1,
            secret,
            lambda _sid, _message: None,
            child_db,
        )

        assert result.value == "applied"
        turn = child_db.execute(
            "SELECT error FROM active_turns WHERE id = ?",
            (identity.turn_id,),
        ).fetchone()
        assert turn["error"] == "Claude Code capability boundary violated"
        rows = child_db.execute(
            "SELECT content FROM messages WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
        assert all(secret not in row["content"] for row in rows)

    @pytest.mark.asyncio
    async def test_mid_turn_input_is_deferred_without_interrupt(
        self, child_db, fake_claude_interruptible,
    ):
        import subagents.cc_runtime as rt

        binary, framelog = fake_claude_interruptible
        _seed_parent(child_db)
        casts: list[tuple[str, dict]] = []
        child_id = await rt.spawn(
            "parent", "HANG-NOW please", binary=binary,
            target=cc_target("sonnet"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        # Turn 1 is streaming (partial committed, no terminal yet).
        assert await _wait(lambda: any(
            sid == child_id and m.get("type") == "step_text"
            and m.get("content") == "partial-one" for sid, m in casts
        )), "turn 1 never streamed its partial"
        assert rt._LIVE.get(child_id).turn_active is True

        with pytest.raises(rt.CCLiveTurnBusy):
            await rt.feed(
                child_id, "second please", target=cc_target("sonnet"),
                turn_id="turn-cc-midturn-successor",
            )

        # A normal input never emits an interrupt control frame or reaches the
        # in-flight process. The shared thread driver retains it in the durable
        # FIFO and claims it after this Turn's terminal event.
        kinds = [f["frame"].get("type") for f in _frames(framelog)]
        assert kinds == ["user"], kinds
        assert rt._LIVE.get(child_id).turn_active is True
        assert _user_rowid(child_db, child_id, "second please") is None

        await rt.retire(child_id)

class TestNaturalCompletionRace:
    @pytest.mark.asyncio
    async def test_busy_input_never_triggers_interrupt_even_if_cli_would_finish(
        self, child_db, fake_claude_natural_race,
    ):
        """Normal input must not manufacture a preempt race with the CLI."""
        import subagents.cc_runtime as rt

        binary, _framelog = fake_claude_natural_race
        _seed_parent(child_db)
        casts: list[tuple[str, dict]] = []
        child_id = await rt.spawn(
            "parent", "HANG-NOW please", binary=binary,
            target=cc_target("sonnet"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        assert await _wait(lambda: any(
            sid == child_id and m.get("type") == "step_text" for sid, m in casts
        ))

        with pytest.raises(rt.CCLiveTurnBusy):
            await rt.feed(
                child_id, "second please", target=cc_target("sonnet"),
                turn_id="turn-cc-natural-race-successor",
            )
        assert [frame["frame"].get("type") for frame in _frames(_framelog)] == [
            "user"
        ]

        await rt.retire(child_id)


class TestHardPreempt:
    @pytest.mark.asyncio
    async def test_hard_interrupt_persists_last_call_occupancy_once(self, child_db):
        from subagents import cc_runtime as rt
        from subagents import cc_preempt
        from subagents.child_thread import persist_assistant_text

        thread_id = "cc-measured-hard-interrupt"
        _seed_parent(child_db, sid=thread_id)
        identity = _admit_active_turn(
            child_db, thread_id, "turn-measured-hard-interrupt",
        )
        assistant_id = persist_assistant_text(
            thread_id, "partial answer",
            turn_id=identity.turn_id,
            db=child_db,
        )
        live = rt._Live(
            session=SimpleNamespace(
                session_id=thread_id,
                transport_generation_id=(
                    "11111111-1111-4111-8111-111111111111"
                ),
            ),
            parent_thread_id=None,
            name="cc",
            model="sonnet",
            catalog_model_id="claude-code/sonnet",
            runtime_context_epoch=2,
            corsia_turn=identity,
            turn_active=True,
            context_window=200_000,
            context_snapshot_id="ctx-measured-hard-interrupt",
            context_snapshot={"snapshot_id": "ctx-measured-hard-interrupt"},
            last_assistant_message_id=assistant_id,
            last_call_usage={
                "input_tokens": 40,
                "cache_creation_input_tokens": 10,
                "cache_read_input_tokens": 2,
            },
        )
        casts: list[tuple[str, dict]] = []
        bcast = lambda sid, message: casts.append((sid, message))

        first = await cc_preempt.commit_interrupted_terminal(
            thread_id,
            live,
            cause="interrupt_timeout",
            bcast=bcast,
            db=child_db,
        )
        duplicate = await cc_preempt.commit_interrupted_terminal(
            thread_id,
            live,
            cause="interrupt_timeout",
            bcast=bcast,
            db=child_db,
        )

        assert first.value == "applied"
        assert duplicate.value == "already_same_outcome"
        terminals = [
            message for sid, message in casts
            if sid == thread_id and message.get("type") == "turn_complete"
        ]
        assert len(terminals) == 1
        assert terminals[0]["context_message_id"] == assistant_id
        assert terminals[0]["context_usage"]["total_used"] == 52
        assert live.last_call_usage is None
        metadata = json.loads(child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND id=?",
            (thread_id, assistant_id),
        ).fetchone()["metadata"])
        assert metadata["context_usage"]["total_used"] == 52
        assert metadata["context_usage"]["harness"] == "claude-code"
        marker = child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='system' "
            "ORDER BY rowid DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        assert json.loads(marker["metadata"])["turn_id"] == identity.turn_id

    @pytest.mark.asyncio
    async def test_hard_interrupt_marker_failure_still_publishes_and_clears_state(
        self, child_db, monkeypatch,
    ):
        from subagents import cc_runtime as rt
        from subagents import cc_preempt
        from subagents.child_thread import persist_assistant_text

        thread_id = "cc-hard-interrupt-marker-failure"
        _seed_parent(child_db, sid=thread_id)
        identity = _admit_active_turn(
            child_db, thread_id, "turn-hard-interrupt-marker-failure",
        )
        assistant_id = persist_assistant_text(
            thread_id, "partial answer",
            turn_id=identity.turn_id,
            db=child_db,
        )
        barrier = asyncio.Event()
        live = rt._Live(
            session=SimpleNamespace(
                session_id=thread_id,
                transport_generation_id=(
                    "22222222-2222-4222-8222-222222222222"
                ),
            ),
            parent_thread_id=None,
            name="cc",
            model="sonnet",
            catalog_model_id="claude-code/sonnet",
            runtime_context_epoch=3,
            corsia_turn=identity,
            turn_active=True,
            turn_terminal=barrier,
            interrupt_requested=True,
            context_window=200_000,
            context_snapshot_id="ctx-hard-interrupt-marker-failure",
            context_snapshot={
                "snapshot_id": "ctx-hard-interrupt-marker-failure",
            },
            last_assistant_message_id=assistant_id,
            last_call_usage={"input_tokens": 23},
        )
        casts: list[tuple[str, dict]] = []

        def fail_marker(*_args, **_kwargs):
            raise RuntimeError("marker insert failed")

        monkeypatch.setattr(cc_preempt, "insert_message", fail_marker)

        first = await cc_preempt.commit_interrupted_terminal(
            thread_id,
            live,
            cause="interrupt_timeout",
            bcast=lambda sid, message: casts.append((sid, message)),
            db=child_db,
        )
        duplicate = await cc_preempt.commit_interrupted_terminal(
            thread_id,
            live,
            cause="interrupt_timeout",
            bcast=lambda sid, message: casts.append((sid, message)),
            db=child_db,
        )

        assert first.value == "applied"
        assert duplicate.value == "already_same_outcome"
        terminals = [
            message for sid, message in casts
            if sid == thread_id and message.get("type") == "turn_complete"
        ]
        assert len(terminals) == 1
        assert terminals[0]["context_message_id"] == assistant_id
        assert terminals[0]["context_usage"]["total_used"] == 23
        metadata = json.loads(child_db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND id=?",
            (thread_id, assistant_id),
        ).fetchone()["metadata"])
        assert metadata["context_usage"]["total_used"] == 23
        assert barrier.is_set()
        assert live.turn_terminal is None
        assert live.turn_active is False
        assert live.interrupt_requested is False
        assert live.last_call_usage is None
        markers = [
            json.loads(row["metadata"])
            for row in child_db.execute(
                "SELECT metadata FROM messages WHERE thread_id = ? "
                "AND role = 'system'",
                (thread_id,),
            ).fetchall()
        ]
        assert not any(marker.get("type") == "result" for marker in markers)

    @pytest.mark.asyncio
    async def test_hard_preempt_replays_observed_natural_exit_without_replacement(
        self, child_db, monkeypatch,
    ):
        from subagents import cc_runtime as rt
        from subagents import cc_preempt
        from subagents.child_thread import persist_assistant_step
        from subagents.cc_runtime_state import PendingProcessExit

        thread_id = "cc-hard-natural-exit-replay"
        _seed_parent(child_db, sid=thread_id)
        identity = _admit_active_turn(
            child_db, thread_id, "turn-hard-natural-exit-replay",
        )

        class AlreadyDeadSession:
            pid = 19

            async def interrupt(self):
                return False

            async def terminate(self):
                return None

        pending_exit = PendingProcessExit(
            identity, 7, "natural exit before hard preempt",
        )
        live = rt._Live(
            session=AlreadyDeadSession(),
            parent_thread_id=None,
            name="cc",
            corsia_turn=identity,
            turn_active=True,
            pending_process_exit=pending_exit,
        )
        step_id = persist_assistant_step(
            thread_id,
            content="",
            tool_uses=[{"id": "call-before-exit", "name": "Read", "arguments": {}}],
            turn_id=identity.turn_id,
            db=child_db,
        )
        live.tool_step_ids["call-before-exit"] = step_id
        live.tool_names["call-before-exit"] = "Read"
        live.tools_settled.clear()
        child_db.commit()
        rt._LIVE.put(thread_id, live)
        replace = AsyncMock(return_value=live)
        monkeypatch.setattr(cc_preempt, "replace_preempted_process", replace)

        preempt = await cc_preempt.preempt_active_turn(
            thread_id,
            live,
            target=cc_target("sonnet"),
            effort=None,
            claude_dir=None,
            binary=None,
            broadcast=None,
            is_subagent=False,
            text="next",
            bcast=lambda *_args: None,
            held_lock=cc_preempt._LIVE.lock(thread_id),
            db=child_db,
        )
        assert preempt is None

        turn = child_db.execute(
            "SELECT status, error FROM active_turns WHERE id=?", (identity.turn_id,),
        ).fetchone()
        assert turn["status"] == "error"
        assert turn["error"] == "natural exit before hard preempt"
        assert rt._LIVE.get(thread_id) is None
        replace.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hard_preempt_drains_buffered_steps_before_terminal(
        self, child_db, monkeypatch,
    ):
        from subagents import cc_runtime as rt
        from subagents import cc_preempt
        from subagents.cc_runtime_events import _make_on_event

        thread_id = "cc-hard-drain-order"
        _seed_parent(child_db, sid=thread_id)
        identity = _admit_active_turn(
            child_db, thread_id, "turn-hard-drain-order",
        )
        casts: list[tuple[str, dict]] = []
        on_event = None

        class BufferedSession:
            pid = 17

            async def interrupt(self):
                return False

            async def terminate(self):
                await on_event({
                    "kind": "assistant_step", "text": "buffered", "reasoning": "",
                    "tool_uses": [{
                        "id": "buffered-1",
                        "name": "mcp__corsia__read_file",
                        "input": {},
                    }],
                })
                # No real result: the preempt fence closed the bridge before this call was declared.

        live = rt._Live(
            session=BufferedSession(), parent_thread_id=None, name="cc",
            corsia_turn=identity, turn_active=True,
        )
        on_event = _make_on_event(
            live, thread_id,
            lambda sid, message: casts.append((sid, message)), child_db,
        )
        monkeypatch.setattr(
            cc_preempt, "replace_preempted_process",
            AsyncMock(return_value=live),
        )

        assert await cc_preempt.preempt_active_turn(
            thread_id, live,
            target=cc_target("sonnet"), effort=None,
            claude_dir=None, binary=None,
            broadcast=None, is_subagent=False, text="next",
            bcast=lambda sid, message: casts.append((sid, message)), db=child_db,
            held_lock=cc_preempt._LIVE.lock(thread_id),
        ) is live

        rows = [dict(row) for row in child_db.execute(
            "SELECT role, metadata, rowid FROM messages WHERE thread_id = ? "
            "ORDER BY rowid",
            (thread_id,),
        ).fetchall()]
        terminal_index = next(
            index for index, row in enumerate(rows)
            if row["role"] == "system"
            and json.loads(row["metadata"] or "{}").get("stop_reason") == "interrupted"
        )
        assert terminal_index == len(rows) - 1
        assert json.loads(rows[0]["metadata"])["event_type"] == "context.digest"
        assert [row["role"] for row in rows[1:terminal_index]] == [
            "user", "assistant", "tool",
        ]
        tool_metadata = json.loads(rows[terminal_index - 1]["metadata"])
        assert tool_metadata["synthetic_tool_result"] is True
        assert tool_metadata["synthetic_reason"] == "provider_missing_result"
        assert sum(message.get("type") == "turn_complete" for _, message in casts) == 1

    @pytest.mark.asyncio
    async def test_hard_preempt_keeps_natural_terminal_drained_from_provider(
        self, child_db, monkeypatch,
    ):
        from subagents import cc_runtime as rt
        from subagents import cc_preempt
        from subagents.cc_runtime_events import _make_on_event

        thread_id = "cc-hard-natural-terminal"
        _seed_parent(child_db, sid=thread_id)
        identity = _admit_active_turn(
            child_db, thread_id, "turn-hard-natural-terminal",
        )
        casts: list[tuple[str, dict]] = []
        completion_calls: list[str] = []
        monkeypatch.setattr(
            "automation.plan_liveness_terminal.on_thread_turn_complete",
            lambda thread_id, db=None, **_: completion_calls.append(thread_id),
        )
        on_event = None

        class NaturallyFinishedSession:
            pid = 18

            async def interrupt(self):
                return False

            async def terminate(self):
                await on_event({
                    "kind": "assistant_step", "text": "finished naturally",
                    "reasoning": "", "tool_uses": [],
                })
                await on_event({
                    "kind": "result", "result": "finished naturally",
                    "is_error": False, "stop_reason": "end_turn",
                })

        live = rt._Live(
            session=NaturallyFinishedSession(), parent_thread_id=None, name="cc",
            corsia_turn=identity, turn_active=True,
        )
        on_event = _make_on_event(
            live, thread_id,
            lambda sid, message: casts.append((sid, message)), child_db,
        )
        monkeypatch.setattr(
            cc_preempt, "replace_preempted_process",
            AsyncMock(return_value=live),
        )

        assert await cc_preempt.preempt_active_turn(
            thread_id, live,
            target=cc_target("sonnet"), effort=None,
            claude_dir=None, binary=None,
            broadcast=None, is_subagent=False, text="next",
            bcast=lambda sid, message: casts.append((sid, message)), db=child_db,
            held_lock=cc_preempt._LIVE.lock(thread_id),
        ) is live

        turn = child_db.execute(
            "SELECT status, error FROM active_turns WHERE id=?", (identity.turn_id,),
        ).fetchone()
        markers = _turn_markers(child_db, thread_id)
        assert tuple(turn) == ("completed", None)
        assert len(markers) == 1
        assert markers[0]["meta"]["is_error"] is False
        assert markers[0]["meta"].get("stop_reason") == "end_turn"
        assert markers[0]["meta"].get("interrupted") is not True
        assert completion_calls == [thread_id]
        assert sum(message.get("type") == "turn_complete" for _, message in casts) == 1

    @pytest.mark.asyncio
    async def test_deaf_cli_busy_input_is_deferred_without_kill(
        self, child_db, fake_claude_deaf, monkeypatch,
    ):
        import subagents.cc_runtime as rt
        import connections.models as cm

        binary, framelog = fake_claude_deaf
        monkeypatch.setattr(
            cm, "get_connection",
            lambda provider, db=None: {"config": {"binary": binary}})
        _seed_parent(child_db)
        casts: list[tuple[str, dict]] = []
        child_id = await rt.spawn(
            "parent", "HANG-NOW please", binary=binary,
            target=cc_target("sonnet"),
            broadcast=lambda sid, m: casts.append((sid, m)),
        )
        assert await _wait(lambda: any(
            sid == child_id and m.get("type") == "step_text"
            and m.get("content") == "partial-one" for sid, m in casts
        ))
        old_pid = rt._LIVE.get(child_id).session.pid
        old_seed = Path(rt._LIVE.get(child_id).seed_file)
        assert old_seed.is_file()
        with pytest.raises(rt.CCLiveTurnBusy):
            await rt.ensure_live_then_feed(
                child_id,
                "second please",
                target=cc_target("sonnet"),
                turn_id="turn-cc-deaf-successor",
                broadcast=lambda sid, msg: casts.append((sid, msg)),
                is_subagent=True,
            )

        frames = _frames(framelog)
        assert [frame["frame"].get("type") for frame in frames] == ["user"]
        assert rt._LIVE.get(child_id).session.pid == old_pid
        assert old_seed.exists(), "normal send must not kill the active process"

        await rt.retire(child_id)

    @pytest.mark.asyncio
    async def test_busy_input_never_attempts_replacement(
        self, child_db, fake_claude_deaf, monkeypatch,
    ):
        """Normal input never invokes the destructive replacement fallback."""
        import subagents.cc_runtime as rt
        from subagents import cc_feed_transition

        binary, _framelog = fake_claude_deaf
        _seed_parent(child_db)
        casts: list[tuple[str, dict]] = []
        child_id = await rt.spawn(
            "parent", "HANG-NOW please", binary=binary,
            target=cc_target("sonnet"),
            broadcast=lambda sid, msg: casts.append((sid, msg)),
        )
        assert await _wait(lambda: any(
            sid == child_id and msg.get("type") == "step_text"
            and msg.get("content") == "partial-one" for sid, msg in casts
        ))
        old_seed = Path(rt._LIVE.get(child_id).seed_file)
        assert old_seed.is_file()
        replacement = AsyncMock()
        monkeypatch.setattr(cc_feed_transition, "resume_child", replacement)
        kwargs = {
            "target": cc_target("sonnet"),
            "turn_id": "turn-cc-failed-replacement-successor",
            "broadcast": lambda sid, msg: casts.append((sid, msg)),
            "is_subagent": True,
        }
        with pytest.raises(rt.CCLiveTurnBusy):
            await rt.ensure_live_then_feed(child_id, "second please", **kwargs)

        replacement.assert_not_awaited()
        assert rt._LIVE.get(child_id) is not None
        assert old_seed.exists(), "normal send must not retire the active process"


@pytest.mark.usefixtures("signed_in_defaults")  # a launch names its account: no ambient login
class TestTerminateDrains:
    @pytest.mark.asyncio
    async def test_reader_failure_cannot_skip_process_reap(
        self, tmp_path, monkeypatch,
    ):
        from subagents.cc_stream import CCStreamSession

        script = tmp_path / "sleep_during_reader_failure"
        script.write_text(
            f"#!{sys.executable}\nimport time\ntime.sleep(60)\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        fail_reader = asyncio.Event()
        session = CCStreamSession(binary=str(script), session_id="failed-reader")

        async def reader_failure():
            await fail_reader.wait()
            raise RuntimeError("reader failed during terminate")

        monkeypatch.setattr(session, "_read_loop", reader_failure)
        session.catalog_model_id = "claude-code/opus"  # a launch names its model
        await session.start()
        terminate_task = asyncio.create_task(session.terminate())
        await asyncio.sleep(0)
        fail_reader.set()
        try:
            await terminate_task
        finally:
            if session.returncode is None:
                session._proc.kill()
            await session._proc.wait()

        assert session.returncode is not None
        assert session._reader.done()

    @pytest.mark.asyncio
    async def test_terminate_reaps_before_propagating_caller_cancellation(
        self, tmp_path,
    ):
        from subagents.cc_stream import CCStreamSession

        script = tmp_path / "event_then_sleep"
        # One write puts both lines in the pipe before the reader can see the
        # first, so the kill can never land between them on a loaded runner.
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys, time\n"
            "sys.stdout.write(''.join(\n"
            " json.dumps({'type': 'assistant', 'message': {'content': "
            "[{'type': 'text', 'text': text}]}}) + '\\n'\n"
            " for text in ('first', 'second')))\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        callback_started = asyncio.Event()
        release_callback = asyncio.Event()
        seen: list[str] = []

        async def on_event(event):
            seen.append(event.get("text", ""))
            if len(seen) == 1:
                callback_started.set()
                await release_callback.wait()

        session = CCStreamSession(
            binary=str(script), session_id="cancelled-caller", on_event=on_event,
        )
        session.catalog_model_id = "claude-code/opus"  # a launch names its model
        await session.start()
        try:
            # A hang guard, not a latency bound: the child interpreter's startup
            # alone can exceed a second on a loaded CI runner (#414).
            await asyncio.wait_for(callback_started.wait(), timeout=30)
            terminate_task = asyncio.create_task(session.terminate())
            await asyncio.sleep(0)
            terminate_task.cancel()
            release_callback.set()
            with pytest.raises(asyncio.CancelledError):
                await terminate_task
            assert seen == ["first", "second"]
            assert session.returncode is not None
            assert session._reader.done() and not session._reader.cancelled()
        finally:
            release_callback.set()
            if session.returncode is None:
                session._proc.kill()
            await session._proc.wait()

    @pytest.mark.asyncio
    async def test_terminate_reaps_process_after_reader_was_cancelled(
        self, tmp_path,
    ):
        from subagents.cc_stream import CCStreamSession

        script = tmp_path / "sleep_until_killed"
        script.write_text(
            f"#!{sys.executable}\nimport time\ntime.sleep(60)\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        session = CCStreamSession(binary=str(script), session_id="cancelled-reader")
        session.catalog_model_id = "claude-code/opus"  # a launch names its model
        await session.start()
        assert session._reader is not None
        session._reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session._reader

        try:
            await session.terminate()
            assert session.returncode is not None
        finally:
            if session.returncode is None:
                session._proc.kill()
            await session._proc.wait()

    @pytest.mark.asyncio
    async def test_terminate_drains_buffered_events_of_exited_process(
        self, tmp_path,
    ):
        """terminate() must mean "no further events will land" — even when
        the process already exited on its own, buffered stdout must be fully
        pumped before terminate returns (hard preempt writes its synthetic
        terminal right after)."""
        import stat as _stat

        from subagents.cc_stream import CCStreamSession

        script = tmp_path / "burst_and_exit"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "for i in range(50):\n"
            "    sys.stdout.write(json.dumps({'type': 'assistant', 'message':"
            " {'content': [{'type': 'text', 'text': 'burst-%d' % i}]}})"
            " + '\\n')\n"
        )
        script.chmod(script.stat().st_mode | _stat.S_IEXEC)

        seen: list[str] = []

        async def on_event(ev):
            if ev.get("kind") == "assistant_step":
                seen.append(ev.get("text", ""))

        session = CCStreamSession(
            binary=str(script), session_id="drain-test", on_event=on_event,
        )
        session.catalog_model_id = "claude-code/opus"  # a launch names its model
        await session.start()
        # Let the process EXIT on its own (returncode set) while its output
        # may still be buffered/pumping.
        await session._proc.wait()

        await session.terminate()
        assert len(seen) == 50, (
            f"terminate returned with only {len(seen)}/50 events drained"
        )
