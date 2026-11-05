"""A native Step that repeats one provider call id always reaches its terminal.

A repeated id cannot name one declared call, so the canonical writer refuses
its result. Whether the batch ran, was stopped before it ran, or was stopped
part-way, the Turn ends in error instead of staying open; no second effect
runs, and a result is kept only when it can be told apart.

Drives the real ``run_tracked_turn`` (finalize, terminal claim, settlement
drive, runtime retire) around the real ``execute_tool_calls`` and Step handler.
Only the provider loop is replaced: it hands one prepared response to the real
batch executor. ``cancelled`` has the stop set before the batch, so the real
``_settle_unexecuted_calls`` closes every call; ``mixed`` runs one unique call
that sets the stop and leaves the two repeated calls to it.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests import native_tool_result_lab as tray
from tools.base import Tool

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


class _Effect(Tool):
    name = "lab_effect"
    description = "record one effect"
    parameters = {"type": "object", "properties": {"tag": {"type": "string"}}}

    def __init__(self):
        self.ran: list[str] = []
        self.stop = None

    async def execute(self, tag="", **_kwargs):
        self.ran.append(tag)
        if tag == "unique" and self.stop is not None:
            self.stop.set()
        return f"effect-result:{tag}"


@pytest.fixture
def lab(tmp_path, monkeypatch, isolated_db):
    lab = tray.install(tmp_path, monkeypatch)
    yield lab
    lab.db.close()


def _response(tool, shape, second_tag):
    calls = [
        SimpleNamespace(id="dup-1", name=tool.name, arguments={"tag": "first"}),
        SimpleNamespace(id="dup-1", name=tool.name, arguments={"tag": second_tag}),
    ]
    if shape == "mixed":
        calls.insert(0, SimpleNamespace(id="unique-1", name=tool.name, arguments={"tag": "unique"}))
    return SimpleNamespace(content="", reasoning_content=None, tool_calls=calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("second_tag", ["second", "first"])
@pytest.mark.parametrize("shape", ["executed", "cancelled", "mixed"])
async def test_repeated_call_id_reaches_an_error_terminal(lab, monkeypatch, shape, second_tag):
    from core import native_turn_runtime
    from core.agent import CORSIAAgent
    from core.native_turn_control import TurnCancelEvent
    from core.step_handler import StepHandler
    from core.turns_steps import execute_tool_calls
    from history.projection import render_portable_history
    from tools.recorded_call import CompletedToolFailure
    from infra import state
    from infra.database import get_db
    from tests.plan_execution_helpers import registry_of, selected_plan

    selected_plan(lab.db, "thread-1")
    lab.db.commit()
    tool, frames = _Effect(), []
    agent = CORSIAAgent(workspace=lab.tmp_path)
    agent.tools = registry_of(tool)

    async def broadcast(frame):
        frames.append(frame)

    handler = StepHandler("thread-1", {"model": "test/model"}, broadcast, lab.identity.turn_id)
    handler.bind_turn_identity(lab.identity)
    stop = TurnCancelEvent()
    stop.bind_turn_identity(lab.identity)
    if shape == "cancelled":
        stop.set()
    if shape == "mixed":
        tool.stop = stop

    async def one_prepared_batch(*, on_step, cancel_event, thread_id, **_kwargs):
        bound_thread = state._current_thread_id.set(thread_id)
        bound_turn = state._current_turn_identity.set(lab.identity)
        try:
            await execute_tool_calls(
                agent, _response(tool, shape, second_tag), [], thread_id, cancel_event,
                on_step, [], "test/model", tool_result_budget_tokens=1000,
            )
        finally:
            state._current_turn_identity.reset(bound_turn)
            state._current_thread_id.reset(bound_thread)

    monkeypatch.setattr(native_turn_runtime, "run_turn_with_steps", one_prepared_batch)
    turn = native_turn_runtime.NativeTurn(
        agent=agent, thread_id="thread-1", content="", model="test/model", turn_provider={},
        handler=handler, cancel_event=stop, identity=lab.identity,
    )
    state._active_turns["thread-1"] = stop
    with pytest.raises(Exception) as raised:  # noqa: B017,PT011 - the type is asserted below
        await native_turn_runtime.run_tracked_turn(turn)
    assert state._active_turns.get("thread-1") is None

    identical = second_tag == "first"
    # Only a batch that ran retains a completed result; a stopped one ran nothing repeated.
    assert isinstance(raised.value, CompletedToolFailure) is (shape == "executed")
    if shape == "executed":
        assert [unit.result for unit in raised.value.completed] == ["effect-result:first"]
    # A refused result is never announced as done.
    assert [f.get("tool_id") for f in frames if f.get("type") == "tool_done"] == (
        ["unique-1"] if shape == "mixed" else []
    )
    assert tool.ran == {"executed": ["first"], "cancelled": [], "mixed": ["unique"]}[shape]
    db = get_db()
    try:
        row = db.execute(
            "SELECT status,ended_at FROM active_turns WHERE id=?", (lab.identity.turn_id,),
        ).fetchone()
        assert row["status"] == "error" and row["ended_at"] is not None
        stored = [r["content"] for r in db.execute(
            "SELECT content FROM messages WHERE thread_id='thread-1' AND role='tool' ORDER BY seq",
        )]
        projection = render_portable_history("thread-1", db=db)
    finally:
        db.close()
    kept = {
        "executed": [] if identical else ["effect-result:first"],
        "cancelled": [],
        "mixed": ["effect-result:unique"],
    }[shape]
    assert stored == kept
    projected = {
        m.tool_call_id: (m.content, m.synthetic_tool_result)
        for t in projection.turns for m in t.messages if m.role == "tool"
    }
    first_kept = shape == "executed" and not identical
    assert projected["dup-1"][1] is (not first_kept)
    assert (projected["dup-1"][0] == "effect-result:first") is first_kept
    if shape == "mixed":
        assert projected["unique-1"] == ("effect-result:unique", False)


def _native_target():
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id="opus", harness="native", provider="anthropic", backend="api",
        transport_model="anthropic/test", expected_reported_model=None,
        context_window=200_000, available=True, availability_reason=None,
        display_name="Test Model", supports_reasoning=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("second_tag", ["second", "first"])
async def test_real_stop_before_any_effect_ends_the_turn(
    isolated_db, monkeypatch, tmp_path, second_tag,
):
    """A user Stop through the ws chain ends the Turn; nothing ran, nothing is stored."""
    from contextlib import closing
    from unittest.mock import AsyncMock

    import ws as ws_mod
    from core import native_turn_runtime
    from core.agent import CORSIAAgent
    from core.turns_steps import execute_tool_calls
    from infra import state
    from infra.database import get_db
    from infra.send_service import send_thread_input
    from shared_send_fixtures import _install_fake_native_context_agent
    from tests.plan_execution_helpers import registry_of, selected_plan
    from ws import turn as ws_turn

    thread_id = f"dup-stop-{second_tag}"
    with closing(get_db()) as db:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,project_id,created_at,updated_at) "
            "VALUES (?,?,'T','opus',NULL,'t','t')", (thread_id, "owner-1"),
        )
        selected_plan(db, thread_id)
        db.commit()
        thread_row = dict(db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())
    tool = _Effect()
    agent = CORSIAAgent(workspace=tmp_path)
    agent.tools = registry_of(tool)
    ready, turn_ids = asyncio.Event(), []

    async def stopped_provider(*, on_step, cancel_event, thread_id, on_provider_response, **_kwargs):
        await on_provider_response()
        identity = cancel_event.turn_identity
        turn_ids.append(identity.turn_id)
        ready.set()
        await cancel_event.wait()
        bound_thread = state._current_thread_id.set(thread_id)
        bound_turn = state._current_turn_identity.set(identity)
        try:
            await execute_tool_calls(
                agent, _response(tool, "cancelled", second_tag), [], thread_id, cancel_event,
                on_step, [], "anthropic/test", tool_result_budget_tokens=1000,
            )
        finally:
            state._current_turn_identity.reset(bound_turn)
            state._current_thread_id.reset(bound_thread)

    _install_fake_native_context_agent(monkeypatch)
    monkeypatch.setattr(ws_turn, "make_provider_params", lambda _model: {})
    monkeypatch.setattr(native_turn_runtime, "run_turn_with_steps", stopped_provider)
    monkeypatch.setattr(ws_turn, "run_post_turn_tasks", AsyncMock())
    monkeypatch.setattr(ws_turn, "emit_product_event", lambda *_a, **_k: None)
    monkeypatch.setattr(state, "broadcast_event", lambda *_a, **_k: None)

    result = await send_thread_input(
        thread_id, thread_row, "go", role="system", metadata={"type": "context_event"},
        native_mode="event", target=_native_target(),
    )
    await asyncio.wait_for(ready.wait(), 10)
    receipt = await asyncio.wait_for(ws_mod._do_stop(
        {"id": "owner-1"}, thread_id, state._active_turns.get(thread_id),
        state._active_llm_tasks.get(thread_id), turn_ids[0],
    ), 30)
    await asyncio.wait_for(asyncio.gather(result.turn_task, return_exceptions=True), 30)

    assert receipt.outcome in {"stopped", "already_terminal"}
    assert tool.ran == []
    assert thread_id not in state._active_turns
    with closing(get_db()) as db:
        turn = db.execute(
            "SELECT status,ended_at FROM active_turns WHERE id=?", (turn_ids[0],),
        ).fetchone()
        assert turn["status"] != "running" and turn["ended_at"] is not None
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='tool'", (thread_id,),
        ).fetchone()[0] == 0
