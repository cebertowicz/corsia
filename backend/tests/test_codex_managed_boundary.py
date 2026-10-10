from __future__ import annotations

import importlib.util
from unittest.mock import AsyncMock

import pytest

from subagents import codex_app_server_events as events
from subagents.codex_app_server import CodexAppServerSession


async def test_app_server_resumes_persisted_managed_thread_without_reregistering_tools() -> None:
    calls: list[tuple[str, dict]] = []
    session = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id="persisted-provider-handle",
        dynamic_tools=[{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read through CORSIA",
                "parameters": {"type": "object"},
            },
        }],
    )

    async def request(method: str, params: dict | None = None):
        calls.append((method, params or {}))
        return {"thread": {"id": "persisted-provider-handle"}}

    session._request = request

    await session._start_or_resume_thread()

    assert calls == [(
        "thread/resume",
        {
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "threadId": "persisted-provider-handle",
            "excludeTurns": True,
        },
    )]
    assert session.thread_id == "persisted-provider-handle"


@pytest.mark.parametrize(
    "item_type",
    [
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "webSearch",
        "imageGeneration",
        "hookExecution",
        "subagentCall",
        "collabAgentToolCall",
        "subAgentActivity",
        "sleep",
    ],
)
def test_every_provider_native_item_is_a_redacted_violation(
    item_type: str,
) -> None:
    secret = "DO_NOT_PERSIST_SECRET"
    item = {
        "id": "provider-item",
        "type": item_type,
        "command": f"cat {secret}",
        "cwd": f"/tmp/{secret}",
        "arguments": {"token": secret},
        "result": secret,
        "error": secret,
    }

    for phase, normalized in (
        ("started", events.item_started_event(item)),
        ("completed", events.item_completed_event(item)),
    ):
        assert normalized == {
            "kind": "provider_tool_violation",
            "provider": "codex",
            "id": "provider-item",
            "tool_type": item_type,
            "phase": phase,
        }
        assert secret not in repr(normalized)


def test_malformed_provider_action_type_is_redacted() -> None:
    secret = "DO_NOT_PERSIST_SECRET"

    normalized = events.item_started_event({
        "id": "provider-item",
        "type": f"malformed-{secret}",
        "arguments": {"token": secret},
    })

    assert normalized == {
        "kind": "provider_tool_violation",
        "provider": "codex",
        "id": "provider-item",
        "tool_type": "unknown-provider-action",
        "phase": "started",
    }
    assert secret not in repr(normalized)


def test_missing_provider_item_type_fails_closed_as_unknown_action() -> None:
    item = {"id": "provider-item", "arguments": {"path": "/tmp/hidden"}}

    assert events.item_started_event(item) == {
        "kind": "provider_tool_violation",
        "provider": "codex",
        "id": "provider-item",
        "tool_type": "unknown-provider-action",
        "phase": "started",
    }
    assert events.item_completed_event(item) == {
        "kind": "provider_tool_violation",
        "provider": "codex",
        "id": "provider-item",
        "tool_type": "unknown-provider-action",
        "phase": "completed",
    }


async def test_native_server_request_is_terminal_without_echoing_payload() -> None:
    emitted: list[dict] = []

    async def on_event(event: dict) -> None:
        emitted.append(event)
        raise RuntimeError("Codex capability boundary violated")

    session = CodexAppServerSession(
        binary="codex",
        task=None,
        codex_thread_id="provider-thread-1",
        on_event=on_event,
    )
    session._track_active_turn("provider-turn-1")
    session._respond = AsyncMock()

    with pytest.raises(RuntimeError, match="capability boundary"):
        await session._handle_server_request({
            "id": 7,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "provider-thread-1",
                "turnId": "provider-turn-1",
                "itemId": "native-action-1",
                "command": "cat DO_NOT_PERSIST_SECRET",
                "cwd": "/Users/example/.ssh",
            },
        })

    assert emitted == [{
        "kind": "provider_tool_violation",
        "provider": "codex",
        "id": 7,
        "tool_type": "item/commandExecution/requestApproval",
        "phase": "request",
        "provider_thread_id": "provider-thread-1",
        "provider_turn_id": "provider-turn-1",
    }]
    session._respond.assert_not_awaited()
    assert "DO_NOT_PERSIST_SECRET" not in repr(emitted)


async def test_auth_refresh_is_the_only_non_dynamic_server_request_allowed() -> None:
    emitted: list[dict] = []

    async def on_event(event: dict) -> None:
        emitted.append(event)

    session = CodexAppServerSession(
        binary="codex",
        task=None,
        on_event=on_event,
    )
    session._respond = AsyncMock()

    await session._handle_server_request({
        "id": 8,
        "method": "account/chatgptAuthTokens/refresh",
        "params": {"token": "DO_NOT_PERSIST_SECRET"},
    })

    assert emitted == []
    session._respond.assert_awaited_once()


def test_legacy_conversation_executor_is_not_importable_or_selectable() -> None:
    from subagents import codex_runtime_config

    assert importlib.util.find_spec("subagents.codex_stream") is None
    assert not hasattr(codex_runtime_config, "codex_app_server_enabled")
