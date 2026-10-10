from __future__ import annotations

from dataclasses import replace

import pytest


SECTIONS = {
    "fixed": 10,
    "checkpoint": 0,
    "tail": 0,
    "current_input": 5,
    "tools": 3,
    "provider_wrapper": 2,
}


def _ledger(*, harness: str = "native"):
    from core.destination_fit import build_destination_fit_ledger

    payload = {
        "messages": [{"role": "user", "content": "FIFO_HEAD"}],
        "tools": [{"name": "read_file"}],
    }
    ledger = build_destination_fit_ledger(
        harness=harness,
        target_catalog_id=f"{harness}/model",
        proven_target_window_tokens=100,
        accounting_payload=payload,
        rendered_input_tokens=sum(SECTIONS.values()),
        section_tokens=SECTIONS,
        input_identity="fifo-head",
        current_input="FIFO_HEAD",
        components={
            "fixed": "system",
            "checkpoint": "",
            "tail": [],
            "current_input": "FIFO_HEAD",
            "tools": payload["tools"],
        },
    )
    return ledger, payload


def test_barrier_consumes_exact_ledger_payload_and_fifo_identity() -> None:
    from core.destination_fit import (
        ProviderCallBlocked,
        require_provider_call_fit,
    )

    ledger, payload = _ledger()
    require_provider_call_fit(
        ledger,
        harness="native",
        target_catalog_id="native/model",
        accounting_payload=payload,
        input_identity="fifo-head",
        current_input="FIFO_HEAD",
    )

    with pytest.raises(ProviderCallBlocked, match="payload"):
        require_provider_call_fit(
            ledger,
            harness="native",
            target_catalog_id="native/model",
            accounting_payload={**payload, "messages": []},
            input_identity="fifo-head",
            current_input="FIFO_HEAD",
        )
    with pytest.raises(ProviderCallBlocked, match="FIFO|input"):
        require_provider_call_fit(
            ledger,
            harness="native",
            target_catalog_id="native/model",
            accounting_payload=payload,
            input_identity="new-head",
            current_input="NEW_HEAD",
        )


def test_barrier_rejects_missing_or_input_overflow_ledger() -> None:
    from core.destination_fit import (
        ProviderCallBlocked,
        require_provider_call_identity,
        require_provider_call_fit,
    )

    with pytest.raises(ProviderCallBlocked, match="ledger"):
        require_provider_call_fit(
            None,
            harness="native",
            target_catalog_id="native/model",
            accounting_payload={},
            input_identity="fifo-head",
            current_input="FIFO_HEAD",
        )

    ledger, payload = _ledger()

    overflow = replace(
        ledger,
        proven_target_window_tokens=19,
        effective_window_tokens=19,
        fits=False,
        block_reason="canonical input exceeds the window",
    )
    require_provider_call_identity(
        overflow,
        harness="native",
        target_catalog_id="native/model",
        accounting_payload=payload,
        input_identity="fifo-head",
        current_input="FIFO_HEAD",
    )
    with pytest.raises(ProviderCallBlocked, match="input|window"):
        require_provider_call_fit(
            overflow,
            harness="native",
            target_catalog_id="native/model",
            accounting_payload=payload,
            input_identity="fifo-head",
            current_input="FIFO_HEAD",
        )

    correlated_fit = replace(overflow, fits=True, block_reason=None)
    require_provider_call_fit(
        correlated_fit,
        harness="native",
        target_catalog_id="native/model",
        accounting_payload=payload,
        input_identity="fifo-head",
        current_input="FIFO_HEAD",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "api_key", "api_base"),
    [
        ("openai/gpt-test", "key", None),
        ("anthropic/claude-test", "key", None),
        ("api/test", "key", "https://example.invalid/v1"),
        ("omlx/test", "omlx", "http://127.0.0.1:8000/v1"),
    ],
)
async def test_native_api_and_omlx_router_seams_refuse_missing_ledger(
    model,
    api_key,
    api_base,
) -> None:
    from core.destination_fit import ProviderCallBlocked
    from llm.router import chat_completion

    with pytest.raises(ProviderCallBlocked, match="ledger|barrier"):
        await chat_completion(
            [{"role": "user", "content": "must not dispatch"}],
            model=model,
            api_key=api_key,
            api_base=api_base,
            max_tokens=32,
            thread_id="active-turn-session",
        )


@pytest.mark.asyncio
async def test_native_iteration_settles_delivery_when_stream_opens(monkeypatch) -> None:
    """A long prefill settles its input before provider deltas arrive."""
    import asyncio
    from types import SimpleNamespace

    from core import turn_iteration, turn_provider_call

    settled: list[str] = []

    async def provider_write(*_args, **kwargs):
        await kwargs["on_response_started"]()
        assert settled == ["accepted"]
        return SimpleNamespace(
            content="done",
            finish_reason="stop",
            has_tool_calls=False,
            usage={},
            ttft_ms=None,
        ), False

    async def accepted():
        settled.append("accepted")

    monkeypatch.setattr(turn_provider_call, "call_llm", provider_write)
    state = SimpleNamespace(
        active_boundary_input=None,
        boundary_write_started=False,
        checkpoint_source_messages=[],
        messages=[],
        tool_definitions=[],
        iteration=1,
        fit_ledger=None,
        fit_current_input="",
        provider_delivery_lock=asyncio.Lock(),
        provider_delivery_settled=False,
        last_measured_call_usage=None,
        last_measured_rendered_input_tokens=None,
        context_usage={},
        context_identity=None,
    )

    result = await turn_iteration._call_provider(
        SimpleNamespace(),
        state,
        "omlx/Fortissimo",
        {},
        "native-prefill",
        asyncio.Event(),
        lambda *_args: None,
        None,
        accepted,
    )

    assert result is not None
    assert settled == ["accepted"]


def _target(harness: str):
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=f"{harness}/model",
        harness=harness,
        provider=harness,
        backend={"claude-code": "cc", "codex": "codex"}[harness],
        transport_model="model",
        expected_reported_model="model",
        context_window=100,
        available=True,
        availability_reason=None,
        display_name="model",
        supports_reasoning=True,
    )


def _prepared_dispatch_context(*, harness: str, stale: bool):
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )

    accounting_payload = {
        "messages": [{"role": "user", "content": "CURRENT_HEAD"}],
        "tools": [],
    }
    ledger = _ledger(harness=harness)[0] if stale else None
    return PreparedContextPayload(
        mode="exact",
        target_catalog_id=f"{harness}/model",
        model_context_window=100,
        history_epoch=0,
        sealed_boundary=None,
        replay=None,
        rendered=RenderedDestinationPayload(
            payload=accounting_payload,
            input_tokens=20,
            accounting_payload=accounting_payload,
            fit_section_tokens=SECTIONS,
            fit_input_identity="current-head",
            fit_current_input="CURRENT_HEAD",
            fit_components={
                "fixed": "system",
                "checkpoint": "",
                "tail": [],
                "current_input": "CURRENT_HEAD",
                "tools": [],
            },
        ),
        fit_ledger=ledger,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["missing", "stale"])
async def test_shared_initial_dispatch_refuses_unproved_feed_before_provider_write(
    monkeypatch,
    proof,
) -> None:
    """The shared dispatch boundary must never enter Codex feed unproved."""
    from infra.send_types import SendRejected
    from infra import send_dispatch
    from subagents import codex_runtime
    from unittest.mock import AsyncMock

    harness = "codex"
    prepared = _prepared_dispatch_context(harness=harness, stale=proof == "stale")
    provider_feed = AsyncMock(
        side_effect=AssertionError("provider feed must not run without exact ledger proof")
    )

    with pytest.raises(SendRejected, match="ledger|fit|context|stale"):
        monkeypatch.setattr(codex_runtime, "ensure_connection_available", lambda: None)
        monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=False))
        monkeypatch.setattr(
            codex_runtime,
            "has_prior_messages",
            AsyncMock(return_value=False),
        )
        monkeypatch.setattr(codex_runtime, "feed_main", provider_feed)
        await send_dispatch.dispatch_codex(
            "thread-1",
            {"project_id": None, "user_id": "user-1"},
            "CURRENT_HEAD",
            "codex_main",
            None,
            target=_target(harness),
            successor_turn_id="turn-1",
            bind_input_turn=AsyncMock(),
            allow_fresh_start=True,
            prepared_context=prepared,
        )

    provider_feed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["feed", "feed_main"])
async def test_cc_runtime_boundary_refuses_unproved_context_before_acceptance(
    monkeypatch,
    entry,
) -> None:
    """Direct CC callers cannot reach acceptance without runtime fit proof."""
    from core.destination_fit import ProviderCallBlocked
    from subagents import cc_feed, cc_runtime
    from subagents.cc_runtime_state import _Live
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    acceptance = AsyncMock(
        side_effect=AssertionError("unproved input reached CC acceptance")
    )
    live = _Live(
        session=SimpleNamespace(returncode=None),
        parent_thread_id=None,
        name="cc",
        model="model",
        catalog_model_id="claude-code/model",
    )
    cc_runtime._LIVE.reset()
    cc_runtime._LIVE.put("thread-1", live)
    monkeypatch.setattr(cc_feed, "accept_reconstructed_input", acceptance)
    kwargs = {
        "target": _target("claude-code"),
        "turn_id": "turn-1",
        "prepared_context": _prepared_dispatch_context(
            harness="claude-code",
            stale=False,
        ),
    }

    with pytest.raises(ProviderCallBlocked, match="ledger|fit|barrier"):
        await getattr(cc_runtime, entry)("thread-1", "CURRENT_HEAD", **kwargs)

    acceptance.assert_not_awaited()
    cc_runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_native_second_loop_requires_fresh_ledger_for_tool_result_payload(
    monkeypatch,
    isolated_db,
) -> None:
    """The second Native write is proved from its new tool-result payload."""
    import hashlib
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core import (
        turn_iteration,
        turn_iteration_context,
        turn_provider_call,
        turns,
        turns_steps,
    )
    from core.destination_preflight import attach_native_provider_fit_ledger
    from core.destination_fit import _digest
    from core.native_turn_control import TurnCancelEvent
    from core.step_handler import StepHandler
    from core.token_estimation import _estimate_tokens
    from core.turn_core import persist_turn_start
    from infra.database import get_db
    from infra.context_receipts import ContextReceiptIdentity
    from infra.thread_driver import admit_input

    tool_definitions = [{"name": "read_file"}]
    initial_messages = [{"role": "user", "content": "FIFO_HEAD"}]
    initial_ledger = attach_native_provider_fit_ledger(
        target_catalog_id="native/model",
        proven_target_window_tokens=100_000,
        messages=initial_messages,
        tools=tool_definitions,
        input_identity="fifo-head",
        current_input="FIFO_HEAD",
    ).fit_ledger

    class Context:
        @staticmethod
        def add_assistant_message(messages, content, tool_calls, **_kwargs):
            return [
                *messages,
                {"role": "assistant", "content": content, "tool_calls": tool_calls},
            ]

        @staticmethod
        def add_tool_result(messages, tool_id, name, result):
            return [
                *messages,
                {
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": result,
                },
            ]

    full_tool_result = "FULL-" + ("oversized-result-" * 20_000)

    class Tools:
        @staticmethod
        def get_definitions():
            return tool_definitions

        @staticmethod
        async def execute(_name, _arguments):
            return full_tool_result

    class Agent:
        model = "native/model"
        max_iterations = 2
        max_tokens = 100
        temperature = 0
        tools = Tools()
        context = Context()
        workspace = Path(".")

        @staticmethod
        async def _connect_mcp():
            return None

        @staticmethod
        def _strip_think(content):
            return content

        @staticmethod
        def _tool_hint(_calls):
            return "read_file"

    responses = iter((
        SimpleNamespace(
            content="",
            finish_reason="tool_calls",
            has_tool_calls=True,
            tool_calls=[
                SimpleNamespace(
                    id="provider-call-1",
                    name="read_file",
                    arguments={"path": "same.md"},
                ),
                SimpleNamespace(
                    id="provider-call-2",
                    name="read_file",
                    arguments={"path": "same.md"},
                ),
            ],
            usage={
                "prompt_tokens": 20,
                "cache_read_tokens": 0,
                "completion_tokens": 3,
            },
            reasoning_content=None,
            reasoning_provenance=None,
            ttft_ms=None,
        ),
        SimpleNamespace(
            content="done",
            finish_reason="stop",
            has_tool_calls=False,
            tool_calls=[],
            usage={
                "prompt_tokens": 30,
                "cache_read_tokens": 0,
                "completion_tokens": 2,
            },
            reasoning_content=None,
            reasoning_provenance=None,
            ttft_ms=None,
        ),
    ))
    calls = []

    async def provider_write(**kwargs):
        calls.append({
            "ledger": kwargs.get("destination_fit_ledger"),
            "messages": kwargs["messages"],
            "tools": kwargs["tools"],
        })
        return next(responses)

    async def no_db(*_args, **_kwargs):
        return None

    monkeypatch.setattr(turn_provider_call, "chat_completion", provider_write)
    monkeypatch.setattr(turns_steps, "lookup_thread_project_id", AsyncMock(return_value=None))
    monkeypatch.setattr(turn_iteration, "run_in_db", no_db)

    prepared = SimpleNamespace(
        messages=tuple(initial_messages),
        context_usage={},
        tool_definitions=tuple(tool_definitions),
        context_identity=ContextReceiptIdentity("native", "native/model", 1),
        fit_ledger=initial_ledger,
    )
    from shared_send_fixtures import _insert_native_model

    _insert_native_model("native/model")
    db = get_db()
    db.execute("UPDATE models SET context_window=100000 WHERE id='native/model'")
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/model','t','t')"
    )
    db.commit()
    admission = admit_input(
        "thread-1",
        role="user",
        content="FIFO_HEAD",
        intent="apply_now",
        message_id="input-1",
        turn_id="turn-1",
        boot_epoch="boot",
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    identity = persist_turn_start(
        "turn-1",
        "thread-1",
        "native/model",
        "FIFO_HEAD",
        db=db,
    )
    db.commit()
    handler = StepHandler(
        "thread-1",
        {"model": "native/model", "model_name": "Native"},
        AsyncMock(),
        "turn-1",
    )
    handler.bind_turn_identity(identity)

    async def on_step(kind, payload):
        return await handler.handle(kind, payload)

    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(identity)

    await turns.run_turn_with_steps(
        Agent(),
        "FIFO_HEAD",
        "thread-1",
        "web",
        "thread-1",
        on_step,
        cancel_event,
        model="native/model",
        turn_provider={"uses_streaming": False, "model": "native/model"},
        model_context_window=100_000,
        prepared_context=prepared,
    )

    assert len(calls) == 2
    assert calls[0]["ledger"] is initial_ledger
    assert calls[1]["ledger"] is not None
    assert calls[1]["ledger"] is not initial_ledger
    assert calls[1]["ledger"].destination_payload_sha256 == _digest({
        "messages": calls[1]["messages"],
        "tools": calls[1]["tools"],
    })
    assert calls[1]["ledger"].destination_payload_sha256 != (
        initial_ledger.destination_payload_sha256
    )
    tool_rows = db.execute(
        "SELECT id,content,metadata FROM messages "
        "WHERE thread_id='thread-1' AND role='tool' ORDER BY seq"
    ).fetchall()
    assert len(tool_rows) == 2
    assert all(row["content"] == full_tool_result for row in tool_rows)
    digest = hashlib.sha256(full_tool_result.encode("utf-8")).hexdigest()
    assert all(
        json.loads(row["metadata"])["tool_result_sha256"] == digest
        for row in tool_rows
    )

    projected = [
        message["content"]
        for message in calls[1]["messages"]
        if message.get("role") == "tool"
    ]
    assert len(projected) == 2
    assert all(value != full_tool_result for value in projected)
    assert all(full_tool_result not in value for value in projected)
    assert all("CORSIA_TOOL_RESULT_PREVIEW" in value for value in projected)
    assert all(digest in value for value in projected)
    assert all(
        row["id"] in value
        for row, value in zip(tool_rows, projected, strict=True)
    )
    initial_remainder = (
        initial_ledger.effective_window_tokens - initial_ledger.rendered_input_tokens
    )
    assert sum(_estimate_tokens(value) for value in projected) <= initial_remainder
    assert calls[1]["ledger"].destination_payload_sha256 == _digest({
        "messages": calls[1]["messages"],
        "tools": calls[1]["tools"],
    })
    db.close()
