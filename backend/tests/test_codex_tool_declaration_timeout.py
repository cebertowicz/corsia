import asyncio
import json

import pytest

from test_tool_result_same_step_binding import _Proc


@pytest.mark.asyncio
async def test_unreleased_declaration_barrier_rejects_and_releases_the_lock(
    isolated_db, monkeypatch,
) -> None:
    from codex_runtime_fixtures import _admit_active_turn
    from shared_send_fixtures import _insert_session
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_event_delivery import (
        arm_delivery_event_barrier,
        runtime_event_handler,
    )
    from subagents.codex_runtime_state import _LiveCodex
    from subagents import codex_tool_requests, harness_tool_bridge

    monkeypatch.setattr(codex_tool_requests, "TOOL_RECEIPT_TIMEOUT_SECONDS", 0.05)
    thread_id = "held-thread"
    provider_thread_id = "held-provider"
    provider_turn_id = "held-provider-turn"
    call_id = "held-call"
    _insert_session(thread_id, model="codex/gpt-5-codex", spawned_by=None)
    identity = _admit_active_turn(thread_id, turn_id="held-turn")
    session = CodexAppServerSession(
        binary="unused", task=None, codex_thread_id=provider_thread_id,
        corsia_thread_id=thread_id, corsia_turn_identity=identity, tool_registry=object(),
    )
    session._proc = _Proc()
    session._track_active_turn(provider_turn_id)
    live = _LiveCodex(
        codex_thread_id=provider_thread_id, session=session, parent_thread_id=None,
        name="codex", corsia_turn=identity,
    )
    live.bind_app_server_session(session)
    session.set_callbacks(on_event=runtime_event_handler(live, thread_id, lambda *_: None))

    async def execute_tool(*_args, **_kwargs) -> str:
        raise AssertionError("an undeclared tool call must be rejected")

    async def receipt(_frame: dict) -> dict:
        return {"last": {"inputTokens": 10, "outputTokens": 1}}

    monkeypatch.setattr(harness_tool_bridge, "execute_tool", execute_tool)
    assert session._tool_requests is not None
    session._tool_requests._usage = receipt
    arm_delivery_event_barrier(live)
    await session._handle_notification("item/started", {
        "threadId": provider_thread_id,
        "turnId": provider_turn_id,
        "item": {"id": call_id, "type": "dynamicToolCall", "tool": "read_file", "arguments": {}},
    })
    await session._handle_message({
        "id": 1,
        "method": "item/tool/call",
        "params": {
            "threadId": provider_thread_id, "turnId": provider_turn_id,
            "callId": call_id, "tool": "read_file", "arguments": {},
        },
    })

    await asyncio.wait_for(session.wait_dynamic_tool_requests_idle(), 1)
    assert not session._tool_requests.lock.locked()
    assert [frame["id"] for frame in session._proc.stdin.frames] == [1]
    assert session._proc.stdin.frames[0]["result"]["success"] is False
    assert "could not be bound" in json.dumps(
        session._proc.stdin.frames[0]["result"],
    ).lower()
