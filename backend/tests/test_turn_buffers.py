
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.state import turn_buffers as stb  # noqa: E402
from core.turn_core import TurnIdentity # noqa: E402

TURN_ID = "turn-1"
TURN = TurnIdentity(TURN_ID, 1)


@pytest.fixture(autouse=True)
def _reset():
    stb._reset_for_tests()
    yield
    stb._reset_for_tests()




def test_init_creates_empty_buffer():
    buf = stb.init("sess-1", TURN)
    assert buf.thread_id == "sess-1"
    assert buf.reasoning == ""
    assert buf.content == ""
    assert buf.active_tools == []
    assert buf.seq == 0
    assert buf.started_at > 0


def test_init_replaces_existing_buffer():
    stb.init("sess-1", TURN)
    stb.append_reasoning("sess-1", TURN, "first")
    stb.init("sess-1", TURN)  # overwrite
    assert stb.get("sess-1", TURN).reasoning == ""
    assert stb.get("sess-1", TURN).seq == 0


def test_stale_generation_cannot_read_mutate_or_clear_successor_buffer():
    stale = TurnIdentity("turn-A", 1)
    winner_identity = TurnIdentity("turn-B", 2)
    stb.init("sess-1", stale)
    stb.append_content("sess-1", stale, "old")
    stb.init("sess-1", winner_identity)
    stb.append_content("sess-1", winner_identity, "winner")
    stb.set_active_tools(
        "sess-1", winner_identity, [{"id": "tool-B", "status": "running"}],
    )

    assert stb.get("sess-1", stale) is None
    assert stb.snapshot("sess-1", stale) is None
    assert stb.append_reasoning("sess-1", stale, "stale") == 0
    assert stb.append_content("sess-1", stale, "stale") == 0
    assert stb.reset_stream("sess-1", stale) == 0
    assert stb.set_active_tools("sess-1", stale, []) == 0
    stb.clear("sess-1", stale)

    winner = stb.snapshot("sess-1", winner_identity)
    assert winner is not None
    assert winner["turn_id"] == "turn-B"
    assert winner["content"] == "winner"
    assert winner["reasoning"] == ""
    assert winner["active_tools"] == [
        {"id": "tool-B", "status": "running"},
    ]


def test_buffer_requires_the_exact_turn_id_and_order():
    owner = TurnIdentity("turn-shared", 10)
    stale = TurnIdentity("turn-shared", 9)
    stb.init("sess-order", owner)

    assert stb.append_content("sess-order", stale, "stale") == 0
    assert stb.snapshot("sess-order", stale) is None
    snapshot = stb.snapshot("sess-order", owner)
    assert snapshot["turn_id"] == owner.turn_id
    assert snapshot["turn_order"] == owner.turn_order


def test_clear_removes_buffer():
    stb.init("sess-1", TURN)
    stb.clear("sess-1", TURN)
    assert stb.get("sess-1", TURN) is None


def test_clear_is_idempotent():
    stb.clear("nonexistent", TURN)  # should not raise
    stb.init("sess-1", TURN)
    stb.clear("sess-1", TURN)
    stb.clear("sess-1", TURN)  # second clear, no error


def test_get_returns_none_for_unknown():
    assert stb.get("nope", TURN) is None


def test_snapshot_returns_none_for_unknown():
    assert stb.snapshot("nope", TURN) is None


def test_snapshot_returns_dict_with_all_fields():
    stb.init("sess-1", TURN)
    stb.append_reasoning("sess-1", TURN, "thinking")
    stb.append_content("sess-1", TURN, "answer")
    stb.set_active_tools(
        "sess-1", TURN, [{"name": "read_file", "status": "running"}],
    )

    snap = stb.snapshot("sess-1", TURN)
    assert snap["thread_id"] == "sess-1"
    assert snap["reasoning"] == "thinking"
    assert snap["content"] == "answer"
    assert snap["active_tools"] == [{"name": "read_file", "status": "running"}]
    assert snap["seq"] == 3  # 3 mutations
    assert snap["started_at"] > 0


def test_snapshot_is_a_copy():
    stb.init("sess-1", TURN)
    stb.set_active_tools("sess-1", TURN, [{"name": "t1"}])

    snap = stb.snapshot("sess-1", TURN)
    snap["reasoning"] = "mutated"
    snap["active_tools"].append({"name": "INJECTED"})

    live = stb.get("sess-1", TURN)
    assert live.reasoning == ""
    assert live.active_tools == [{"name": "t1"}]


def test_active_session_ids_lists_current_buffers():
    stb.init("sess-1", TURN)
    stb.init("sess-2", TURN)
    ids = stb.active_session_ids()
    assert set(ids) == {"sess-1", "sess-2"}




def test_append_reasoning_increments_seq():
    stb.init("sess-1", TURN)
    assert stb.append_reasoning("sess-1", TURN, "a") == 1
    assert stb.append_reasoning("sess-1", TURN, "b") == 2
    assert stb.append_reasoning("sess-1", TURN, "c") == 3
    assert stb.get("sess-1", TURN).reasoning == "abc"


def test_append_content_increments_seq():
    stb.init("sess-1", TURN)
    stb.append_reasoning("sess-1", TURN, "r")
    assert stb.append_content("sess-1", TURN, "c1") == 2
    assert stb.append_content("sess-1", TURN, "c2") == 3
    assert stb.get("sess-1", TURN).content == "c1c2"


def test_set_active_tools_increments_seq():
    stb.init("sess-1", TURN)
    assert stb.set_active_tools("sess-1", TURN, [{"name": "x"}]) == 1
    assert stb.set_active_tools("sess-1", TURN, [{"name": "y"}]) == 2
    assert stb.get("sess-1", TURN).active_tools == [{"name": "y"}]


def test_set_active_tools_with_empty_list_still_increments_seq():
    stb.init("sess-1", TURN)
    stb.set_active_tools("sess-1", TURN, [{"name": "x"}])
    assert stb.set_active_tools("sess-1", TURN, []) == 2
    assert stb.get("sess-1", TURN).active_tools == []


def test_append_to_unknown_session_returns_zero():
    assert stb.append_reasoning("nope", TURN, "x") == 0
    assert stb.append_content("nope", TURN, "x") == 0
    assert stb.set_active_tools("nope", TURN, []) == 0


def test_append_empty_chunk_is_noop():
    stb.init("sess-1", TURN)
    assert stb.append_reasoning("sess-1", TURN, "") == 0
    assert stb.append_content("sess-1", TURN, "") == 0
    assert stb.get("sess-1", TURN).seq == 0




def test_reset_stream_clears_reasoning_and_content_keeps_tools():
    stb.init("sess-1", TURN)
    stb.append_reasoning("sess-1", TURN, "thinking about X")
    stb.append_content("sess-1", TURN, "answer part 1")
    stb.set_active_tools("sess-1", TURN, [{"name": "read_file"}])

    new_seq = stb.reset_stream("sess-1", TURN)
    assert new_seq == 4  # prior 3 mutations + this reset
    buf = stb.get("sess-1", TURN)
    assert buf.reasoning == ""
    assert buf.content == ""
    assert buf.active_tools == [{"name": "read_file"}]  # untouched


def test_reset_stream_on_unknown_session_returns_zero():
    assert stb.reset_stream("nope", TURN) == 0


def test_reset_stream_then_append_works_normally():
    stb.init("sess-1", TURN)
    stb.append_content("sess-1", TURN, "step1 content")
    stb.reset_stream("sess-1", TURN)
    stb.append_content("sess-1", TURN, "step2 content")
    assert stb.get("sess-1", TURN).content == "step2 content"


def test_finalize_step_consumes_only_explicit_tool_ids():
    stb.init("sess-1", TURN)
    stb.append_reasoning("sess-1", TURN, "thinking")
    stb.append_content("sess-1", TURN, "draft")
    stb.set_active_tools("sess-1", TURN, [
        {"id": "tool-a", "status": "done"},
        {"id": "tool-b", "status": "running"},
    ])

    seq = stb.finalize_step("sess-1", TURN, ["tool-a"])

    assert seq == 4
    buf = stb.get("sess-1", TURN)
    assert buf.reasoning == ""
    assert buf.content == ""
    assert buf.active_tools == [{"id": "tool-b", "status": "running"}]


def test_finalize_step_without_ids_keeps_active_tools():
    stb.init("sess-1", TURN)
    stb.set_active_tools("sess-1", TURN, [{"id": "tool-a"}])

    assert stb.finalize_step("sess-1", TURN, []) == 2
    assert stb.get("sess-1", TURN).active_tools == [{"id": "tool-a"}]




def test_sweep_evicts_stale_buffer_with_no_active_turn(monkeypatch):
    from infra import state
    monkeypatch.setattr(state, "_active_turns", {}, raising=False)

    buf = stb.init("sess-stale", TURN)
    buf.started_at = time.time() - (stb.MAX_TURN_DURATION_SEC + 60)

    assert stb._sweep_once() == 1
    assert stb.get("sess-stale", TURN) is None


def test_sweep_leaves_stale_buffer_with_active_turn(monkeypatch):
    from infra import state
    monkeypatch.setattr(state, "_active_turns", {"sess-still-running": asyncio.Event()}, raising=False)

    buf = stb.init("sess-still-running", TURN)
    buf.started_at = time.time() - (stb.MAX_TURN_DURATION_SEC + 60)

    assert stb._sweep_once() == 0
    assert stb.get("sess-still-running", TURN) is not None


def test_sweep_leaves_fresh_buffer(monkeypatch):
    from infra import state
    monkeypatch.setattr(state, "_active_turns", {}, raising=False)

    stb.init("sess-fresh", TURN)  # started_at = now
    assert stb._sweep_once() == 0
    assert stb.get("sess-fresh", TURN) is not None


def test_sweep_handles_multiple_buffers(monkeypatch):
    from infra import state
    monkeypatch.setattr(state, "_active_turns", {"active": asyncio.Event()}, raising=False)

    stb.init("fresh", TURN)
    stb.init("active", TURN).started_at = time.time() - (stb.MAX_TURN_DURATION_SEC + 60)
    stb.init("dead", TURN).started_at = time.time() - (stb.MAX_TURN_DURATION_SEC + 60)

    assert stb._sweep_once() == 1
    assert stb.get("fresh", TURN) is not None
    assert stb.get("active", TURN) is not None
    assert stb.get("dead", TURN) is None




@pytest.mark.asyncio
async def test_start_sweeper_returns_task_and_is_idempotent():
    t1 = stb.start_sweeper()
    t2 = stb.start_sweeper()
    assert t1 is t2  # same task reused
    assert not t1.done()
    stb.stop_sweeper()
    try:
        await asyncio.wait_for(t1, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


@pytest.mark.asyncio
async def test_stop_sweeper_cancels_running_task():
    task = stb.start_sweeper()
    stb.stop_sweeper()
    new_task = stb.start_sweeper()
    assert new_task is not task
    stb.stop_sweeper()
    await asyncio.sleep(0.01)  # let cancellation land


@pytest.mark.asyncio
async def test_stop_sweeper_when_not_running_is_noop():
    stb.stop_sweeper()  # no task started
    stb.stop_sweeper()  # second call also fine


def test_tool_frames_keep_the_row_label_and_outcome_for_a_reconnect():
    from ws.turn_helpers import _update_tool_buffer

    stb.init("sess-1", TURN)
    _update_tool_buffer("sess-1", TURN, {
        "type": "tool_start", "tool_id": "t1", "name": "read_file",
        "phrase": "Reading", "args": '{"pa',
    })
    _update_tool_buffer("sess-1", TURN, {
        "type": "tool_done", "tool_id": "t1", "name": "read_file",
        "phrase": "Reading a.md", "object": "/w/a.md", "outcome": "failed",
        "error_line": "Error: no such file", "result": "Error: no such file",
    })
    tool = stb.snapshot("sess-1", TURN)["active_tools"][0]
    assert (tool["phrase"], tool["object"], tool["outcome"], tool["error_line"]) == (
        "Reading a.md", "/w/a.md", "failed", "Error: no such file",
    )
