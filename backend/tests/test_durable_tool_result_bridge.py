from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from infra.default_port import DEFAULT_BACKEND_PORT


@pytest.mark.asyncio
async def test_cc_rotation_retry_reuses_one_explicit_invocation_id(monkeypatch) -> None:
    from subagents import corsia_mcp_server

    posts: list[dict] = []
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        if len(posts) == 1:
            return httpx.Response(
                401,
                json={"detail": corsia_mcp_server.TOOL_TOKEN_ROTATED_DETAIL},
            )
        return httpx.Response(200, text="result")

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(corsia_mcp_server.asyncio, "sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await corsia_mcp_server._call_tool(
            client,
            f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}/api/cc/_tool-bridge/thread-1",
            "read_file",
            {"path": "README.md"},
        )

    assert result == "result"
    assert sleeps == [corsia_mcp_server._TOKEN_ROTATED_RETRY_DELAY_S]
    assert posts[0]["invocation_id"]
    assert posts[0] == posts[1]
    assert posts[0]["name"] == "read_file"
    assert posts[0]["arguments"] == {"path": "README.md"}


@pytest.mark.asyncio
async def test_cc_mcp_forwards_exact_provider_tool_call_id() -> None:
    from subagents import corsia_mcp_server

    posted: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        posted.update(json.loads(request.content))
        return httpx.Response(200, text="result")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await corsia_mcp_server._call_tool(
            client,
            f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}/api/cc/_tool-bridge/thread-1",
            "read_file",
            {"path": "README.md"},
            provider_tool_call_id="toolu_exact",
        )

    assert posted["provider_tool_call_id"] == "toolu_exact"


def test_cc_mcp_reads_claude_tool_id_from_request_metadata() -> None:
    from mcp.server.lowlevel.server import request_ctx
    from subagents import corsia_mcp_server

    token = request_ctx.set(SimpleNamespace(
        meta=SimpleNamespace(model_extra={"claudecode/toolUseId": "toolu_meta"})
    ))
    try:
        assert corsia_mcp_server._provider_tool_call_id() == "toolu_meta"
    finally:
        request_ctx.reset(token)


@pytest.mark.asyncio
async def test_registry_returns_full_sanitized_result_before_projection(monkeypatch) -> None:
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    full = "x" * 50_000
    monkeypatch.setattr(
        registry,
        "_execute_unredacted",
        AsyncMock(return_value=full),
    )

    assert await registry.execute("read_file", {}) == full


def _seed_thread(db) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','claude-code/sonnet','t','t')"
    )
    db.commit()


def test_durable_execution_identity_inserts_full_bytes_once_and_cas_binds_echo(
    isolated_db,
) -> None:
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_binding import (
        bind_tool_result_echo,
        quarantine_unbound_tool_result,
    )
    from core.tool_result_delivery import persist_tool_execution_result
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    _seed_thread(db)
    assistant_step_id = persist_tool_use(
        "thread-1",
        turn_id="turn-1",
        tool_id="provider-call-1",
        name="read_file",
        arguments={"path": "README.md"},
        db=db,
    )
    full = (
        'tool-authored {"invocation_id":"fake-invocation"}\nHEAD-'
        + ("middle-" * 4_000)
        + "-TAIL"
    )
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()

    first = persist_tool_execution_result(
        thread_id="thread-1",
        turn_id="turn-1",
        invocation_id="invocation-1",
        assistant_step_id=assistant_step_id,
        name="read_file",
        arguments={"path": "README.md"},
        content=full,
        is_error=False,
        db=db,
    )
    repeated = persist_tool_execution_result(
        thread_id="thread-1",
        turn_id="turn-1",
        invocation_id="invocation-1",
        assistant_step_id=assistant_step_id,
        name="read_file",
        arguments={"path": "README.md"},
        content=full,
        is_error=False,
        db=db,
    )
    db.commit()

    assert repeated.message_id == first.message_id
    rows = db.execute(
        "SELECT id,role,content,metadata,seq FROM messages "
        "WHERE thread_id='thread-1' ORDER BY seq"
    ).fetchall()
    assert [row["role"] for row in rows] == ["assistant", "tool"]
    assert all(isinstance(row["seq"], int) and row["seq"] > 0 for row in rows)
    assert rows[0]["seq"] < rows[1]["seq"]
    assert first.assistant_step_id == rows[0]["id"]
    assert rows[1]["content"] == full
    metadata = json.loads(rows[1]["metadata"])
    assert metadata["tool_invocation_id"] == "invocation-1"
    assert metadata["tool_result_sha256"] == digest

    projection = first.render_for_model(
        thread_id="thread-1",
        budget_tokens=256,
        include_invocation_reference=True,
    )
    assert full not in projection
    assert '"invocation_id":"invocation-1"' in projection

    quarantined = quarantine_unbound_tool_result(
        thread_id="thread-1",
        message_id=first.message_id,
        db=db,
    )
    assert quarantined.message_id == first.message_id
    quarantined_metadata = json.loads(db.execute(
        "SELECT metadata FROM messages WHERE id=?",
        (first.message_id,),
    ).fetchone()["metadata"])
    assert quarantined_metadata["tool_result_binding_state"] == "quarantined_unbound"
    assert "tool_call_id" not in quarantined_metadata

    bound = bind_tool_result_echo(
        thread_id="thread-1",
        provider_tool_call_id="provider-call-1",
        assistant_step_id=first.assistant_step_id,
        echoed_content=projection,
        db=db,
    )
    repeated_bound = bind_tool_result_echo(
        thread_id="thread-1",
        provider_tool_call_id="provider-call-1",
        assistant_step_id=first.assistant_step_id,
        echoed_content=projection,
        db=db,
    )
    db.commit()

    assert bound.message_id == first.message_id
    assert repeated_bound.message_id == first.message_id
    rows = db.execute(
        "SELECT id,role,content,metadata,seq FROM messages "
        "WHERE thread_id='thread-1' ORDER BY seq"
    ).fetchall()
    assert [row["role"] for row in rows] == ["assistant", "tool"]
    assert rows[1]["content"] == full
    metadata = json.loads(rows[1]["metadata"])
    assert metadata["tool_call_id"] == "provider-call-1"
    assert metadata["assistant_step_id"] == first.assistant_step_id
    assert metadata["tool_result_binding_state"] == "bound"

    with pytest.raises(ToolResultBindingError, match="different provider|already bound"):
        bind_tool_result_echo(
            thread_id="thread-1",
            provider_tool_call_id="provider-call-2",
            assistant_step_id=first.assistant_step_id,
            echoed_content=projection,
            db=db,
        )
    db.close()


@pytest.mark.parametrize(
    "conflict",
    ("arguments", "provider_call", "turn"),
)
def test_execution_retry_requires_exact_canonical_lineage(
    isolated_db,
    conflict,
) -> None:
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_delivery import persist_tool_execution_result
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    _seed_thread(db)
    assistant_step_id = persist_tool_use(
        "thread-1",
        turn_id="turn-1",
        tool_id="provider-call-1",
        name="read_file",
        arguments={"path": "README.md"},
        provider_turn_id="provider-turn-1",
        db=db,
    )
    assert persist_tool_use(
        "thread-1",
        turn_id="turn-1",
        tool_id="provider-call-2",
        name="read_file",
        arguments={"path": "README.md"},
        provider_turn_id="provider-turn-1",
        assistant_step_id=assistant_step_id,
        db=db,
    ) == assistant_step_id
    request = {
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "invocation_id": "invocation-1",
        "assistant_step_id": assistant_step_id,
        "provider_tool_call_id": "provider-call-1",
        "name": "read_file",
        "arguments": {"path": "README.md"},
        "content": "exact result",
        "is_error": False,
        "db": db,
    }
    first = persist_tool_execution_result(**request)
    repeated = persist_tool_execution_result(**request)
    assert repeated.message_id == first.message_id

    conflicting = dict(request)
    if conflict == "arguments":
        conflicting["arguments"] = {"path": "CHANGED.md"}
    elif conflict == "provider_call":
        conflicting["provider_tool_call_id"] = "provider-call-2"
    else:
        conflicting["turn_id"] = "turn-2"
    with pytest.raises(ToolResultBindingError):
        persist_tool_execution_result(**conflicting)

    rows = db.execute(
        "SELECT metadata FROM messages "
        "WHERE thread_id='thread-1' AND role='tool' ORDER BY seq"
    ).fetchall()
    assert len(rows) == 1
    metadata = json.loads(rows[0]["metadata"])
    assert metadata["turn_id"] == "turn-1"
    assert metadata["arguments"] == {"path": "README.md"}
    assert metadata["provider_tool_call_id"] == "provider-call-1"
    db.close()


# Real CC/Codex transcript reuse is exercised through the bridge race below.


def test_execution_persistence_refuses_missing_provider_assistant_step(
    isolated_db,
) -> None:
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_delivery import persist_tool_execution_result
    from infra.database import get_db

    db = get_db()
    _seed_thread(db)
    with pytest.raises(ToolResultBindingError, match="Assistant Step|predecessor"):
        persist_tool_execution_result(
            thread_id="thread-1",
            turn_id="turn-1",
            invocation_id="invocation-1",
            assistant_step_id=None,
            name="read_file",
            arguments={"path": "README.md"},
            content="must not persist",
            is_error=False,
            db=db,
        )
    assert db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id='thread-1'"
    ).fetchone()[0] == 0
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["cc", "codex"])
async def test_parallel_identical_real_bridge_calls_bind_reversed_echoes(
    isolated_db,
    harness,
    monkeypatch,
) -> None:
    from infra.thread_driver import admit_input
    from infra.database import get_db

    class OrderedRegistry:
        def __init__(self) -> None:
            self.results = iter(("full result 1", "full result 2"))

        def get(self, _name):
            return None

        async def execute(self, _name, _arguments):
            return next(self.results)

    db = get_db()
    model = "claude-code/sonnet" if harness == "cc" else "codex/gpt"
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1',?,'t','t')",
        (model,),
    )
    db.commit()
    admission = admit_input(
        "thread-1",
        role="user",
        content="run tools",
        intent="apply_now",
        message_id="turn-1-input",
        turn_id="turn-1",
        boot_epoch="test",
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    identity = admission.batch.identity
    from infra.input_writes import begin_batch_write, settle_batch_write

    begin_batch_write(
        admission.batch,
        attempt_id="turn-1-write",
        started_at="t",
        db=db,
    )
    settle_batch_write("turn-1-write", outcome="accepted", db=db)
    db.commit()
    registry = OrderedRegistry()

    boundary_calls = []

    async def no_checkpoint_boundary(*args, **kwargs):
        boundary_calls.append((args, kwargs))
        return False

    monkeypatch.setattr(
        "subagents.harness_checkpoint_resume.maintain_external_tool_boundary",
        no_checkpoint_boundary,
    )

    if harness == "cc":
        from api import cc_attach
        from starlette.requests import Request
        from subagents.cc_runtime_events import _make_on_event
        from subagents.cc_runtime_state import _LIVE, _Live
        from subagents.cc_stream import CCStreamSession

        cc_session = CCStreamSession(binary="unused", session_id="fixture")
        cc_process = SimpleNamespace(
            returncode=None,
            stdout=SimpleNamespace(at_eof=lambda: False),
        )
        cc_process.kill = lambda: setattr(cc_process, "returncode", -9)
        cc_session._proc = cc_process
        cc_session.tool_context.observe({"type": "assistant", "message": {
            "id": "receipt-1", "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 5000},
            "content": [{"type": "tool_use", "id": f"provider-call-{number}"} for number in (1, 2)],
        }})
        from test_external_tool_projection import target

        monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target(100_000))

        live = _Live(
            session=cc_session,
            parent_thread_id=None,
            name="cc",
            corsia_turn=identity,
            tool_registry=registry,
            tool_token="live-secret",
        )
        live.turn_active = True
        live.last_call_usage = {"input_tokens": 100}
        live.context_window = 100_000
        live.catalog_model_id = model
        _LIVE.put("thread-1", live)
        on_event = _make_on_event(live, "thread-1", lambda *_args: None, db)
        await on_event({
            "kind": "assistant_step",
            "text": "",
            "reasoning": "",
            "tool_uses": [
                {
                    "id": "provider-call-1",
                    "name": "mcp__corsia__read_file",
                    "input": {"path": "same.md"},
                },
                {
                    "id": "provider-call-2",
                    "name": "mcp__corsia__read_file",
                    "input": {"path": "same.md"},
                },
            ],
        })
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/cc/_tool-bridge/thread-1",
            "raw_path": b"/api/cc/_tool-bridge/thread-1",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 9000),
            "server": ("127.0.0.1", DEFAULT_BACKEND_PORT),
        }
        projections = {}
        for number in (2, 1):
            response = await cc_attach.execute_cc_tool(
                Request(scope),
                "thread-1",
                    {
                        "invocation_id": f"invocation-{number}",
                        "provider_tool_call_id": f"provider-call-{number}",
                        "name": "read_file",
                    "arguments": {"path": "same.md"},
                },
                "live-secret",
            )
            chunk = await anext(response.body_iterator)
            projections[number] = (
                chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            )
    else:
        from subagents.codex_app_server import CodexAppServerSession
        from subagents.codex_runtime_events import make_on_event
        from subagents.codex_runtime_state import _LIVE, _LiveCodex

        app_session = CodexAppServerSession(
            binary="codex",
            task=None,
            tool_registry=registry,
            corsia_thread_id="thread-1",
            corsia_turn_identity=identity,
            model=model,
        )
        app_session.thread_id = "provider-thread-1"
        app_session._track_active_turn("provider-turn-1")
        app_session._usage_by_turn["provider-turn-1"] = {
            "last": {"inputTokens": 100, "cachedInputTokens": 0, "outputTokens": 5000},
            "total": {"inputTokens": 19000000, "outputTokens": 20000},
            "modelContextWindow": 100_000,
        }
        from test_external_tool_projection import target

        monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target(100_000))
        live = _LiveCodex(
            codex_thread_id="provider-thread-1",
            session=app_session,
            parent_thread_id=None,
            name="codex",
            corsia_turn=identity,
            model=model,
            catalog_model_id=model,
            context_window=100_000,
        )
        live.bind_app_server_session(app_session)
        _LIVE.put("thread-1", live)
        on_event = make_on_event(
            live,
            "thread-1",
            lambda *_args: None,
            dead_set=set(),
            thread_registry={},
        )
        for number in (1, 2):
            await on_event({
                "kind": "tool_use",
                "id": f"provider-call-{number}",
                "name": "read_file",
                "input": {"path": "same.md"},
                "provider_thread_id": "provider-thread-1",
                "provider_turn_id": "provider-turn-1",
            })
        projections = {}
        for number in (1, 2):
            result, error = await app_session._handle_dynamic_tool_request({
                "threadId": "provider-thread-1",
                "turnId": "provider-turn-1",
                "callId": f"provider-call-{number}",
                "tool": "read_file",
                "arguments": {"path": "same.md"},
            })
            assert error is None
            projections[number] = result["contentItems"][0]["text"]

    invocation_prefix = "invocation" if harness == "cc" else "provider-call"
    assert f'"invocation_id":"{invocation_prefix}-1"' in projections[1]
    assert f'"invocation_id":"{invocation_prefix}-2"' in projections[2]
    assert len(boundary_calls) == 1
    # Current 100 input + 5000 output, including hidden reasoning, plus
    # both final replies and invocation markers. Never lifetime volume.
    assert 5150 < boundary_calls[0][1].get("complete_input_tokens", 0) < 5400
    if harness == "cc":
        assert len(boundary_calls) == 1
        args, kwargs = boundary_calls[0]
        assert args[2] == "claude-code"
        assert kwargs["current_tool_call_id"] in {
            "provider-call-1", "provider-call-2",
        }
        committed = db.execute(
            "SELECT metadata FROM messages "
            "WHERE thread_id='thread-1' AND role='tool' ORDER BY seq"
        ).fetchall()
        committed_meta = [json.loads(row["metadata"]) for row in committed]
        assert [item["tool_invocation_id"] for item in committed_meta] == [
            "invocation-2",
            "invocation-1",
        ]
        assert [item["tool_call_id"] for item in committed_meta] == [
            "provider-call-2",
            "provider-call-1",
        ]
        assert all(item["tool_result_binding_state"] == "bound" for item in committed_meta)
    for number in (1, 2):
        event = {
            "kind": "tool_result",
            "tool_use_id": f"provider-call-{number}",
            "content": projections[number],
        }
        if harness == "codex":
            event.update({
                "provider_thread_id": "provider-thread-1",
                "provider_turn_id": "provider-turn-1",
            })
        await on_event(event)

    rows = db.execute(
        "SELECT role,content,metadata,seq FROM messages "
        "WHERE thread_id='thread-1' ORDER BY seq"
    ).fetchall()
    assert json.loads(rows[0]["metadata"])["event_type"] == "context.digest"
    assert [row["role"] for row in rows[1:]] == [
        "user", "assistant", "tool", "tool",
    ]
    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
    assert [row["content"] for row in rows[3:]] == [
        "full result 1",
        "full result 2",
    ]
    assistant_meta = json.loads(rows[2]["metadata"])
    assert [call["id"] for call in assistant_meta["llm_tool_calls"]] == [
        "provider-call-1",
        "provider-call-2",
    ]
    result_metadata = [json.loads(row["metadata"]) for row in rows[3:]]
    assert all(
        item["tool_result_binding_state"] == "bound"
        for item in result_metadata
    )
    by_provider_id = {
        metadata["tool_call_id"]: row["content"]
        for metadata, row in zip(result_metadata, rows[3:], strict=True)
    }
    assert by_provider_id == (
        {
            "provider-call-1": "full result 2",
            "provider-call-2": "full result 1",
        }
        if harness == "cc"
        else {
            "provider-call-1": "full result 1",
            "provider-call-2": "full result 2",
        }
    )
    _LIVE.pop("thread-1", None)
    db.close()


@pytest.mark.asyncio
async def test_codex_dynamic_request_binds_provider_call_id_to_invocation(monkeypatch) -> None:
    from subagents.codex_app_server import CodexAppServerSession

    execute = AsyncMock(return_value="bounded result")
    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    identity = SimpleNamespace(turn_id="corsia-turn-1")
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        tool_registry=object(),
        corsia_thread_id="thread-1",
        corsia_turn_identity=identity,
    )
    session.declared_tool_step_ids = {"provider-call-1": "assistant-step-1"}

    result, error = await session._handle_dynamic_tool_request({
        "tool": "read_file",
        "arguments": {"path": "README.md"},
        "callId": "provider-call-1",
        "threadId": "provider-thread-1",
        "turnId": "provider-turn-1",
    })

    assert error is None
    assert result == {
        "contentItems": [{"type": "inputText", "text": "bounded result"}],
        "success": True,
    }
    execute.assert_awaited_once_with(
        session.tool_registry,
        thread_id="thread-1",
        turn_identity=identity,
        invocation_id="provider-call-1",
        provider_tool_call_id="provider-call-1",
        assistant_step_id="assistant-step-1",
        name="read_file",
        arguments={"path": "README.md"},
        harness="codex",
    )


@pytest.mark.asyncio
async def test_codex_checkpoint_tool_uses_scope_without_live_turn_settlement(
    monkeypatch,
) -> None:
    from subagents.codex_app_server import CodexAppServerSession

    execute = AsyncMock(return_value="checkpoint tool result")
    settle = AsyncMock()
    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    monkeypatch.setattr("subagents.harness_tool_bridge.settle_codex_tool_result", settle)
    session = CodexAppServerSession(
        binary="codex", task=None, tool_registry=object(),
        corsia_thread_id="thread-1", fork_id="cp-1",
    )
    assert session.declared_tool_step_ids == {}

    result, error = await session._handle_dynamic_tool_request({
        "tool": "read_file", "arguments": {"path": "README.md"},
        "callId": "call-1",
    })

    assert error is None and result["success"] is True
    call = execute.await_args
    assert call is not None
    assert call.kwargs["fork_id"] == "cp-1"
    assert call.kwargs["turn_identity"] is None
    assert call.kwargs["assistant_step_id"] is None
    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_foreground_tool_rejects_an_empty_step_map(monkeypatch) -> None:
    from core._tool_result_delivery_support import ToolResultBindingError
    from subagents.codex_app_server import CodexAppServerSession

    execute = AsyncMock()
    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    session = CodexAppServerSession(
        binary="codex", task=None, tool_registry=object(),
        corsia_thread_id="thread-1", corsia_turn_identity=SimpleNamespace(turn_id="turn-1"),
    )

    with pytest.raises(ToolResultBindingError, match="exact Assistant Step"):
        await session._handle_dynamic_tool_request({
            "tool": "read_file", "arguments": {"path": "README.md"}, "callId": "call-1",
        })

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_stale_dynamic_tool_handle_cannot_continue_provider(
    monkeypatch,
) -> None:
    from core._tool_result_delivery_support import ToolExecutionResult
    from subagents.codex_app_server import CodexAppServerSession
    from subagents.codex_runtime_state import _LIVE, _LiveCodex
    from subagents.harness_tool_bridge import ToolBridgeResult
    from tools.recorded_call import CompletedToolFailure

    identity = SimpleNamespace(turn_id="corsia-turn-1")
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        tool_registry=object(),
        corsia_thread_id="thread-1",
        corsia_turn_identity=identity,
        model="codex/gpt",
    )
    session.thread_id = "provider-thread-1"
    session._track_active_turn("provider-turn-1")
    session._usage_by_turn["provider-turn-1"] = {
        "last": {"inputTokens": 10},
        "modelContextWindow": 100_000,
    }
    session.declared_tool_step_ids = {"provider-call-1": "assistant-1"}
    delivery = ToolExecutionResult(
        message_id="result-1",
        assistant_step_id="assistant-1",
        invocation_id="provider-call-1",
        provider_tool_call_id="provider-call-1",
        name="read_file",
        content="full result",
        content_sha256=hashlib.sha256(b"full result").hexdigest(),
        content_byte_count=len(b"full result"),
        is_error=False,
    )
    execute = AsyncMock(return_value=ToolBridgeResult("bounded result", delivery))
    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    replacement = _LiveCodex(
        codex_thread_id="provider-thread-2",
        session=object(),
        parent_thread_id=None,
        name="replacement",
        corsia_turn=identity,
        model="codex/gpt",
        catalog_model_id="codex/gpt",
        context_window=100_000,
    )
    _LIVE.put("thread-1", replacement)
    try:
        with pytest.raises(CompletedToolFailure) as failure:
            await session._handle_dynamic_tool_request({
                "tool": "read_file",
                "arguments": {"path": "README.md"},
                "callId": "provider-call-1",
                "threadId": "provider-thread-1",
                "turnId": "provider-turn-1",
            })
    finally:
        _LIVE.pop("thread-1", None)
    execute.assert_awaited_once()
    assert failure.value.completed[0].result.delivery is delivery


def test_bind_tool_result_echo_settles_undispatched_call_for_any_provider(
    isolated_db,
) -> None:
    """The zero-row shortcut is provider-agnostic: it is keyed only on the
    declaring Assistant Step and provider_tool_call_id, exactly what a Codex
    ``_on_tool_result`` echo supplies, not on a Claude Code-specific signal."""
    from core.tool_result_binding import bind_tool_result_echo
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    _seed_thread(db)
    assistant_step_id = persist_tool_use(
        "thread-1",
        turn_id="turn-1",
        tool_id="codex-call-1",
        name="write_file",
        arguments={"path": "one"},
        db=db,
    )

    bound = bind_tool_result_echo(
        thread_id="thread-1",
        provider_tool_call_id="codex-call-1",
        assistant_step_id=assistant_step_id,
        echoed_content="No such tool available: write_file",
        db=db,
    )
    db.commit()

    assert bound.provider_tool_call_id == "codex-call-1"
    row = db.execute(
        "SELECT metadata FROM messages WHERE id=?", (bound.message_id,),
    ).fetchone()
    metadata = json.loads(row["metadata"])
    assert metadata["synthetic_reason"] == "undispatched_tool"
    assert metadata["synthetic_tool_result"] is True


def test_late_result_cannot_replace_terminal_synthetic_result(isolated_db) -> None:
    from core._tool_result_delivery_support import ToolResultBindingError
    from core.tool_result_delivery import (
        persist_tool_execution_result,
        synthetic_result_identity,
    )
    from infra.delivery_states import PROVIDER_MISSING_RESULT_REASON
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    _seed_thread(db)
    step_id = persist_tool_use(
        "thread-1",
        turn_id="turn-1",
        tool_id="provider-call-1",
        name="read_file",
        arguments={"path": "README.md"},
        db=db,
    )
    invocation_id, extra_metadata = synthetic_result_identity(
        tool_use_id="provider-call-1",
        reason=PROVIDER_MISSING_RESULT_REASON,
    )
    persist_tool_execution_result(
        thread_id="thread-1",
        turn_id="turn-1",
        invocation_id=invocation_id,
        assistant_step_id=step_id,
        provider_tool_call_id="provider-call-1",
        name="read_file",
        arguments={"path": "README.md"},
        content="provider result missing",
        is_error=True,
        extra_metadata=extra_metadata,
        db=db,
    )
    query_plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM messages WHERE thread_id=? "
        "AND role='tool' AND json_valid(metadata) "
        "AND json_extract(metadata,'$.assistant_step_id')=? "
        "AND json_extract(metadata,'$.tool_call_id')=? "
        "AND json_extract(metadata,'$.synthetic_tool_result')=1",
        ("thread-1", step_id, "provider-call-1"),
    ).fetchall()
    assert any("idx_messages_tool_lineage" in str(row[-1]) for row in query_plan)

    with pytest.raises(ToolResultBindingError, match="terminal synthetic"):
        persist_tool_execution_result(
            thread_id="thread-1",
            turn_id="turn-1",
            invocation_id="provider:provider-call-1",
            assistant_step_id=step_id,
            provider_tool_call_id="provider-call-1",
            name="read_file",
            arguments={"path": "README.md"},
            content="late real result",
            is_error=False,
            db=db,
        )

    count = db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='tool'",
        ("thread-1",),
    ).fetchone()[0]
    assert count == 1
    db.close()
