from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


async def _ignore_broadcast(_payload: dict) -> None:
    return None


def _seed_session(thread_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, created_at, updated_at) VALUES (?,?,?,?,?)",
            (thread_id, "owner", "omlx@local/test", "t", "t"),
        )
        db.commit()
    finally:
        db.close()


def _message_metadata(thread_id: str) -> list[tuple[str, dict]]:
    from infra.database import get_db

    db = get_db()
    try:
        rows = db.execute(
            "SELECT role, metadata FROM messages "
            "WHERE thread_id = ? ORDER BY seq",
            (thread_id,),
        ).fetchall()
        return [
            (row["role"], json.loads(row["metadata"] or "{}"))
            for row in rows
        ]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_native_assistant_row_uses_bound_turn_over_spoofed_extra_meta(
    isolated_db,
):
    from core.step_handler import StepHandler
    from core.turn_core import TurnIdentity

    thread_id = "native-assistant-turn-binding"
    turn_id = "11111111-1111-4111-8111-111111111111"
    _seed_session(thread_id)
    handler = StepHandler(
        thread_id=thread_id,
        model_info={"model": "test/model", "model_name": "Test"},
        broadcast=_ignore_broadcast,
        turn_id=turn_id,
        extra_meta={"turn_id": "caller-spoofed-turn"},
    )
    handler.bind_turn_identity(TurnIdentity(turn_id, 1))

    await handler.handle("text", {"content": "answer"})

    rows = _message_metadata(thread_id)
    assert [role for role, _meta in rows] == ["assistant"]
    assert rows[0][1]["turn_id"] == turn_id


@pytest.mark.asyncio
async def test_native_tool_batch_rows_use_bound_turn_over_spoofed_extra_meta(
    isolated_db,
):
    from core.step_handler import StepHandler
    from core.turn_core import TurnIdentity

    thread_id = "native-tool-turn-binding"
    turn_id = "22222222-2222-4222-8222-222222222222"
    _seed_session(thread_id)
    handler = StepHandler(
        thread_id=thread_id,
        model_info={"model": "test/model", "model_name": "Test"},
        broadcast=_ignore_broadcast,
        turn_id=turn_id,
        extra_meta={"turn_id": "caller-spoofed-turn"},
    )
    handler.bind_turn_identity(TurnIdentity(turn_id, 1))

    await handler.handle("tool_calls_start", {
        "tools": [{
            "id": "call-1", "name": "Read", "hint": "read", "args": "{}",
        }],
        "content": "",
        "tool_call_dicts": [{
            "id": "call-1",
            "type": "function",
            "function": {"name": "Read", "arguments": "{}"},
        }],
    })
    await handler.handle("tool_complete", {
        "id": "call-1", "name": "Read", "result": "contents",
    })

    rows = _message_metadata(thread_id)
    assert [role for role, _meta in rows] == ["assistant", "tool"]
    assert [meta["turn_id"] for _role, meta in rows] == [turn_id, turn_id]


@pytest.mark.parametrize(
    "helper_name",
    [
        "record_user_frame",
        "persist_assistant_step",
        "persist_assistant_text",
        "persist_tool_use",
        "persist_tool_result",
    ],
)
def test_live_child_persistence_requires_explicit_turn_id(helper_name: str):
    from subagents import child_thread

    parameter = inspect.signature(getattr(child_thread, helper_name)).parameters.get(
        "turn_id"
    )
    assert parameter is not None, f"{helper_name} has no turn_id contract"
    assert parameter.default is inspect.Parameter.empty


def test_live_child_persistence_stores_one_turn_id_across_all_roles(isolated_db):
    from subagents.child_thread import (
        persist_assistant_text,
        persist_tool_result,
        persist_tool_use,
        record_user_frame,
    )

    thread_id = "external-live-turn-binding"
    turn_id = "33333333-3333-4333-8333-333333333333"
    _seed_session(thread_id)
    try:
        record_user_frame(thread_id, "question", turn_id=turn_id)
        persist_assistant_text(thread_id, "checking", turn_id=turn_id)
        step_id = persist_tool_use(
            thread_id,
            tool_id="tool-1",
            name="Read",
            arguments={"file_path": "/tmp/a"},
            turn_id=turn_id,
        )
        persist_tool_result(
            thread_id,
            tool_use_id="tool-1",
            name="Read",
            content="contents",
            assistant_step_id=step_id,
            turn_id=turn_id,
        )
    except TypeError as exc:
        pytest.fail(f"live persistence rejected explicit turn_id: {exc}")

    rows = _message_metadata(thread_id)
    assert [role for role, _meta in rows] == [
        "user", "assistant", "assistant", "tool",
    ]
    assert all(meta["turn_id"] == turn_id for _role, meta in rows)


@pytest.mark.asyncio
async def test_cc_live_events_forward_exact_turn_id_to_all_durable_steps(monkeypatch):
    from core.turn_core import TurnIdentity
    from subagents import cc_event_steps, cc_event_tool_settlement, cc_runtime_events
    from subagents.cc_runtime_state import _Live

    identity = TurnIdentity("44444444-4444-4444-8444-444444444444", 7)
    live = _Live(
        session=None,
        parent_thread_id=None,
        name="cc",
        model="sonnet",
        corsia_turn=identity,
    )
    persisted: list[tuple[str, dict]] = []

    def _persist_assistant(*_args, **kwargs):
        persisted.append(("assistant", kwargs))
        return "step-1"

    def _persist_result(*_args, **kwargs):
        persisted.append(("tool", kwargs))
        return "result-1"

    def _bind_echo(**kwargs):
        # Under the execution-time persistence contract the echo binds to an
        # already-durable canonical row; record the binding identity here.
        persisted.append(("tool_echo", kwargs))
        from core._tool_result_delivery_support import ToolExecutionResult

        return ToolExecutionResult(
            message_id="result-1",
            assistant_step_id=kwargs["assistant_step_id"],
            invocation_id="echo-invocation",
            provider_tool_call_id=kwargs["provider_tool_call_id"],
            name="mcp__corsia__read_file",
            content=kwargs["echoed_content"],
            content_sha256="sha",
            content_byte_count=len(kwargs["echoed_content"]),
            is_error=False,
        )

    monkeypatch.setattr(cc_event_steps, "bind_tool_result_echo", _bind_echo)

    monkeypatch.setattr(
        cc_event_steps, "persist_assistant_step", _persist_assistant,
    )
    monkeypatch.setattr(
        cc_event_tool_settlement, "persist_tool_result", _persist_result,
    )
    monkeypatch.setattr(
        cc_event_steps,
        "load_assistant_step_display",
        lambda *_args, **_kwargs: {
            "content": "", "metadata": {"tool_calls": []}, "created_at": "t",
        },
    )
    monkeypatch.setattr(
        cc_event_steps, "observe_tool_result", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        cc_event_steps, "emit_finalized_step", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        cc_event_tool_settlement,
        "emit_finalized_step",
        AsyncMock(return_value=None),
    )
    on_event = cc_runtime_events._make_on_event(
        live,
        "cc-live-binding",
        lambda _thread_id, _payload: None,
        None,
    )

    await on_event({
        "kind": "assistant_step",
        "text": "",
        "tool_uses": [{
            "id": "tool-1",
            "name": "mcp__corsia__read_file",
            "input": {},
        }],
    })
    await on_event({
        "kind": "tool_result",
        "tool_use_id": "tool-1",
        "content": "contents",
    })
    live.tool_names["tool-2"] = "mcp__corsia__read_file"
    live.tool_step_ids["tool-2"] = "step-2"
    await cc_event_tool_settlement.settle_missing_tool_results(
        live=live,
        child_id="cc-live-binding",
        identity=identity,
        bcast=lambda _thread_id, _payload: None,
        db=None,
    )

    assert [kind for kind, _kwargs in persisted] == [
        "assistant", "tool_echo", "tool",
    ]
    assert all(
        kwargs["turn_id"] == identity.turn_id
        for kind, kwargs in persisted
        if kind != "tool_echo"
    )
    assert persisted[1][1]["assistant_step_id"] == "step-1"
    assert persisted[-1][1]["synthetic_tool_result"] is True


@pytest.mark.asyncio
async def test_codex_live_events_forward_exact_turn_id_to_all_durable_steps(
    monkeypatch,
):
    from core.turn_core import TurnIdentity
    from subagents import child_thread, codex_runtime_events
    from subagents.codex_runtime_state import _LiveCodex

    identity = TurnIdentity("55555555-5555-4555-8555-555555555555", 8)
    live = _LiveCodex(
        codex_thread_id="provider-thread",
        session=SimpleNamespace(active_turn_id="provider-turn"),
        parent_thread_id=None,
        name="codex",
        model="gpt-5.6-sol",
        corsia_turn=identity,
    )
    persisted: list[tuple[str, dict]] = []

    def _persist_assistant(*_args, **kwargs):
        persisted.append(("assistant", kwargs))
        return "step-text"

    def _persist_tool_use(*_args, **kwargs):
        persisted.append(("tool_use", kwargs))
        return "step-tool"

    def _persist_result(*_args, **kwargs):
        persisted.append(("tool_result", kwargs))
        return "result-1"

    def _bind_echo(**kwargs):
        # Under the execution-time persistence contract the echo binds to an
        # already-durable canonical row; record the binding identity here.
        persisted.append(("tool_echo", kwargs))
        from core._tool_result_delivery_support import ToolExecutionResult

        return ToolExecutionResult(
            message_id="result-1",
            assistant_step_id=kwargs["assistant_step_id"],
            invocation_id="echo-invocation",
            provider_tool_call_id=kwargs["provider_tool_call_id"],
            name="Read",
            content=kwargs["echoed_content"],
            content_sha256="sha",
            content_byte_count=len(kwargs["echoed_content"]),
            is_error=False,
        )

    monkeypatch.setattr(
        "core.tool_result_binding.bind_tool_result_echo", _bind_echo,
    )

    async def _run_now(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(child_thread, "persist_assistant_text", _persist_assistant)
    monkeypatch.setattr(child_thread, "persist_tool_use", _persist_tool_use)
    monkeypatch.setattr(child_thread, "persist_tool_result", _persist_result)
    monkeypatch.setattr(
        child_thread,
        "load_assistant_step_display",
        lambda *_args, **_kwargs: {
            "content": "", "metadata": {"tool_calls": []}, "created_at": "t",
        },
    )
    monkeypatch.setattr(codex_runtime_events, "run_in_db", _run_now)
    monkeypatch.setattr(
        codex_runtime_events, "_observe_tool_result", AsyncMock(return_value=None),
    )
    on_event = codex_runtime_events.make_on_event(
        live,
        "codex-live-binding",
        lambda _thread_id, _payload: None,
        dead_set=set(),
        thread_registry={"codex-live-binding": "provider-thread"},
    )

    await on_event({"kind": "assistant_text", "text": "answer"})
    await on_event({
        "kind": "tool_use", "id": "tool-1", "name": "Read", "input": {},
        "provider_thread_id": "provider-thread",
        "provider_turn_id": "provider-turn",
    })
    await on_event({
        "kind": "tool_result", "tool_use_id": "tool-1", "content": "contents",
        "provider_thread_id": "provider-thread",
        "provider_turn_id": "provider-turn",
    })
    live.tool_names["tool-2"] = "Read"
    live.tool_step_ids["tool-2"] = "step-2"
    await codex_runtime_events.settle_missing_tool_results(
        live=live,
        thread_id="codex-live-binding",
        identity=identity,
        bcast=lambda _thread_id, _payload: None,
    )

    assert [kind for kind, _kwargs in persisted] == [
        "assistant", "tool_use", "tool_echo", "tool_result",
    ]
    assert all(
        kwargs["turn_id"] == identity.turn_id
        for kind, kwargs in persisted
        if kind != "tool_echo"
    )
    assert persisted[2][1]["assistant_step_id"] == "step-tool"
    assert persisted[-1][1]["synthetic_tool_result"] is True
