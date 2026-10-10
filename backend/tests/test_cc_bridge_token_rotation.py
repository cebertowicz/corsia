"""Tool-bridge token rotation: bounded dual-accept grace window (issue #825).

A live-handle transition revokes the current tool token and installs a fresh
one on the successor. These tests pin the contract that a call issued under
the immediately-previous token survives the rotation (or fails with the exact
named condition), without weakening auth for foreign or expired tokens.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from cc_runtime_fixtures import trust_live_tool_step_ids
from infra.default_port import DEFAULT_BACKEND_PORT

_SESSION = "thread-rot"
_BRIDGE_PATH = f"/api/cc/_tool-bridge/{_SESSION}"
_CALL = {
    "name": "read_file",
    "arguments": {"path": "README.md"},
    "provider_tool_call_id": "call-1",
}


@pytest.fixture(autouse=True)
def _clear_rotation_state(monkeypatch):
    from subagents.cc_runtime_state import _LIVE, reset_tool_token_grace

    # This suite owns bridge authentication; capacity/settlement has its own
    # real-transport tests and is outside the synthetic successor's contract.
    monkeypatch.setattr("subagents.harness_tool_bridge.settle_cc_tool_result", AsyncMock())
    trust_live_tool_step_ids(monkeypatch)
    _LIVE.pop(_SESSION, None)
    reset_tool_token_grace()
    yield
    _LIVE.pop(_SESSION, None)
    reset_tool_token_grace()


def _successor(registry, **overrides):
    """A settled successor live handle that admits tool execution."""
    from core._tool_result_delivery_support import ToolExecutionResult
    from subagents.harness_tool_bridge import ToolBridgeResult

    _identity = SimpleNamespace(turn_id="turn-1")
    _delivery = ToolExecutionResult(
        message_id="msg-1",
        assistant_step_id="step-1",
        invocation_id="call-1",
        provider_tool_call_id="call-1",
        name="read_file",
        content="exact result",
        content_sha256="",
        content_byte_count=len("exact result"),
        is_error=False,
    )
    _mock_result = ToolBridgeResult("exact result", _delivery)
    fields = {
        "tool_registry": registry,
        "tool_definitions": (
            {"type": "function", "function": {"name": "read_file"}},
        ),
        "tool_token": "current-secret",
        "tool_step_ids": {"call-1": "step-1"},
        "corsia_turn": _identity,
        "admitted_tool_tasks": set(),
        "turn_active": True,
        "interrupt_requested": False,
    }
    fields.update(overrides)
    live = SimpleNamespace(**fields)
    live._mock_result = _mock_result
    return live


async def _client():
    from api import cc_attach as route_module

    app = FastAPI()
    app.include_router(route_module.router)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 9000))
    return httpx.AsyncClient(transport=transport, base_url="http://corsia")


def _header(token: str) -> dict[str, str]:
    return {"X-CORSIA-Tool-Token": token}


@pytest.mark.asyncio
async def test_rotation_mid_call_succeeds_under_grace(monkeypatch):
    """A call dispatched under the predecessor token lands after rotation."""
    from api import cc_attach as route_module
    from subagents.cc_runtime_state import _LIVE, record_tool_token_rotation

    registry = object()
    record_tool_token_rotation(_SESSION, "old-secret")
    live = _successor(registry)
    _LIVE.put(_SESSION, live)
    execute = AsyncMock(return_value=live._mock_result)
    monkeypatch.setattr(route_module, "execute_tool", execute)
    from subagents import cc_event_tool_settlement

    monkeypatch.setattr(
        cc_event_tool_settlement,
        "settle_committed_tool_result",
        AsyncMock(return_value=False),
    )

    async with await _client() as client:
        listed = await client.get(_BRIDGE_PATH, headers=_header("old-secret"))
        called = await client.post(
            _BRIDGE_PATH, headers=_header("old-secret"), json=_CALL,
        )

    assert listed.status_code == 200
    assert listed.json() == {
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
    }
    assert called.status_code == 200
    assert called.text == "exact result"
    call_kwargs = execute.await_args.kwargs
    assert call_kwargs["thread_id"] == _SESSION
    assert call_kwargs["name"] == "read_file"
    assert call_kwargs["arguments"] == {"path": "README.md"}


@pytest.mark.asyncio
async def test_grace_calls_face_the_same_admission_gates(monkeypatch):
    """Grace only authenticates; a turn-inactive successor still refuses."""
    from api import cc_attach as route_module
    from subagents.cc_runtime_state import _LIVE, record_tool_token_rotation

    record_tool_token_rotation(_SESSION, "old-secret")
    _LIVE.put(_SESSION, _successor(object(), turn_active=False))
    execute = AsyncMock(return_value="must not execute")
    monkeypatch.setattr(route_module, "execute_tool", execute)

    async with await _client() as client:
        called = await client.post(
            _BRIDGE_PATH, headers=_header("old-secret"), json=_CALL,
        )

    assert called.status_code == 409
    assert called.json() == {
        "detail": (
            "Claude Code tools are unavailable outside an active provider turn"
        ),
    }
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_predecessor_token_expires_after_the_grace_window(monkeypatch):
    from api import cc_attach as route_module
    from subagents.cc_runtime_state import (
        _LIVE,
        record_tool_token_rotation,
        session_recently_rotated,
    )

    record_tool_token_rotation(
        _SESSION, "old-secret", now=time.monotonic() - 31.0,
    )
    _LIVE.put(_SESSION, _successor(object()))
    execute = AsyncMock(return_value="must not execute")
    monkeypatch.setattr(route_module, "execute_tool", execute)

    async with await _client() as client:
        called = await client.post(
            _BRIDGE_PATH, headers=_header("old-secret"), json=_CALL,
        )

    # Past the window the predecessor is indistinguishable from a foreign
    # token, and the auth read swept its record.
    assert called.status_code == 401
    assert called.json() == {"detail": "Invalid Claude Code tool token"}
    assert session_recently_rotated(_SESSION) is False
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_back_to_back_rotations_invalidate_the_grandparent(monkeypatch):
    from api import cc_attach as route_module
    from subagents.cc_runtime_state import _LIVE, record_tool_token_rotation

    record_tool_token_rotation(_SESSION, "grandparent-secret")
    record_tool_token_rotation(_SESSION, "parent-secret")
    live = _successor(object())
    _LIVE.put(_SESSION, live)
    execute = AsyncMock(return_value=live._mock_result)
    monkeypatch.setattr(route_module, "execute_tool", execute)
    from subagents import cc_event_tool_settlement

    monkeypatch.setattr(
        cc_event_tool_settlement,
        "settle_committed_tool_result",
        AsyncMock(return_value=False),
    )

    async with await _client() as client:
        grandparent = await client.post(
            _BRIDGE_PATH, headers=_header("grandparent-secret"), json=_CALL,
        )
        parent = await client.post(
            _BRIDGE_PATH, headers=_header("parent-secret"), json=_CALL,
        )

    assert grandparent.status_code == 401
    assert grandparent.json() == {"detail": "Invalid Claude Code tool token"}
    assert parent.status_code == 200
    assert parent.text == "exact result"
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejections_name_the_exact_condition(monkeypatch):
    from api import cc_attach as route_module
    from subagents.cc_runtime_state import (
        _LIVE,
        record_tool_token_rotation,
        reset_tool_token_grace,
    )

    execute = AsyncMock(return_value="must not execute")
    monkeypatch.setattr(route_module, "execute_tool", execute)

    # Foreign token while a handle is live: unchanged bare invalid-token 401.
    _LIVE.put(_SESSION, _successor(object()))
    async with await _client() as client:
        foreign = await client.post(
            _BRIDGE_PATH, headers=_header("garbage"), json=_CALL,
        )
    assert foreign.status_code == 401
    assert foreign.json() == {"detail": "Invalid Claude Code tool token"}

    # Mid-transition gap: handle popped, rotation recorded. The predecessor
    # token is told the token rotated (retryable); a foreign token only
    # learns the session has no live handle.
    _LIVE.pop(_SESSION, None)
    record_tool_token_rotation(_SESSION, "old-secret")
    async with await _client() as client:
        predecessor = await client.post(
            _BRIDGE_PATH, headers=_header("old-secret"), json=_CALL,
        )
        gap_foreign = await client.post(
            _BRIDGE_PATH, headers=_header("garbage"), json=_CALL,
        )
    assert predecessor.status_code == 401
    assert predecessor.json() == {"detail": "Claude Code tool token rotated"}
    assert gap_foreign.status_code == 401
    assert gap_foreign.json() == {
        "detail": "Claude Code session has no live handle",
    }

    # No live handle and no rotation record: the session is unknown here.
    reset_tool_token_grace()
    async with await _client() as client:
        unknown = await client.post(
            _BRIDGE_PATH, headers=_header("old-secret"), json=_CALL,
        )
    assert unknown.status_code == 401
    assert unknown.json() == {"detail": "Unknown Claude Code session"}
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_then_rebuild_window_settles_into_grace(monkeypatch):
    """The full transition arc: revoke -> rotated 401 -> successor accepts."""
    from api import cc_attach as route_module
    from subagents.cc_runtime_quiescence import revoke_tool_access
    from subagents.cc_runtime_state import (
        _LIVE,
        _Live,
        is_grace_tool_token,
    )

    # Pre-create the successor so the mock can reference its result.
    successor = _successor(object())
    execute = AsyncMock(return_value=successor._mock_result)
    monkeypatch.setattr(route_module, "execute_tool", execute)
    from subagents import cc_event_tool_settlement

    monkeypatch.setattr(
        cc_event_tool_settlement,
        "settle_committed_tool_result",
        AsyncMock(return_value=False),
    )

    old = _Live(
        session=None,
        parent_thread_id=None,
        name="cc",
        tool_registry=object(),
        tool_token="old-secret",
    )
    _LIVE.put(_SESSION, old)

    revoke_tool_access(old, thread_id=_SESSION)

    assert old.tool_token == ""
    assert old.tool_registry is None
    assert is_grace_tool_token(_SESSION, "old-secret") is True

    # Inside the revoke-then-rebuild window the predecessor call is named a
    # rotation (client may retry once), never a bare auth failure or a 503.
    async with await _client() as client:
        mid_transition = await client.post(
            _BRIDGE_PATH, headers=_header("old-secret"), json=_CALL,
        )
    assert mid_transition.status_code == 401
    assert mid_transition.json() == {"detail": "Claude Code tool token rotated"}

    # Once the successor settles, the same retried call executes under it.
    _LIVE.put(_SESSION, successor)
    async with await _client() as client:
        retried = await client.post(
            _BRIDGE_PATH, headers=_header("old-secret"), json=_CALL,
        )
    assert retried.status_code == 200
    assert retried.text == "exact result"
    execute.assert_awaited_once()


def test_configure_records_a_replaced_in_place_token(monkeypatch):
    from subagents.cc_runtime_state import is_grace_tool_token
    from subagents.harness_tool_bridge import configure_cc_tool_bridge

    monkeypatch.setenv("BACKEND_PORT", str(DEFAULT_BACKEND_PORT))
    live = SimpleNamespace(tool_token="prev-secret")

    config = configure_cc_tool_bridge(
        live,
        thread_id=_SESSION,
        working_dir=None,
        registry=object(),
    )

    try:
        assert is_grace_tool_token(_SESSION, "prev-secret") is True
        assert live.tool_token
        assert live.tool_token != "prev-secret"
    finally:
        os.unlink(config)


def test_rotation_records_are_bounded():
    from subagents.cc_runtime_state import (
        is_grace_tool_token,
        record_tool_token_rotation,
        session_recently_rotated,
    )

    now = time.monotonic()
    record_tool_token_rotation(_SESSION, "t1", now=now)
    assert is_grace_tool_token(_SESSION, "t1", now=now + 29.9) is True
    assert is_grace_tool_token(_SESSION, "t1", now=now + 30.0) is False

    # A later write sweeps expired records from other sessions.
    record_tool_token_rotation("thread-other", "t2", now=now + 31.0)
    assert session_recently_rotated(_SESSION, now=now + 31.0) is False
    assert session_recently_rotated("thread-other", now=now + 31.0) is True
    record_tool_token_rotation("thread-other", "", now=now + 31.0)
    assert is_grace_tool_token("thread-other", "t2", now=now + 31.0) is True

    from subagents.cc_runtime_state import reset_tool_token_grace

    reset_tool_token_grace()
    assert session_recently_rotated("thread-other") is False


def test_rotate_once_then_idle_record_is_swept_by_a_read():
    """A session that rotates once and dies leaves nothing after expiry."""
    from subagents.cc_runtime_state import (
        _TOOL_TOKEN_GRACE,
        is_grace_tool_token,
        record_tool_token_rotation,
    )

    now = time.monotonic()
    record_tool_token_rotation(_SESSION, "only-rotation", now=now - 31.0)
    assert _SESSION in _TOOL_TOKEN_GRACE

    # Any auth read — even for an unrelated session — sweeps the expired
    # record; no later rotation anywhere is required.
    assert is_grace_tool_token("thread-unrelated", "whatever", now=now) is False
    assert _TOOL_TOKEN_GRACE == {}


@pytest.mark.asyncio
async def test_terminal_close_purges_the_rotation_record():
    """A handle popped for good takes its session's grace record with it."""
    from subagents import cc_runtime_lifecycle as lifecycle
    from subagents.cc_runtime_state import _LIVE, _Live, session_recently_rotated

    class DrainedSession:
        mcp_config = None
        returncode = 0

        async def close(self):
            return self.returncode

    live = _Live(
        session=DrainedSession(),
        parent_thread_id=None,
        name="cc",
        tool_registry=object(),
        tool_token="final-secret",
    )
    _LIVE.put(_SESSION, live)

    assert await lifecycle.retire_live(_SESSION, lambda *_args: None) is True

    assert _LIVE.get(_SESSION) is None
    assert live.tool_token == ""
    assert session_recently_rotated(_SESSION) is False


@pytest.mark.asyncio
async def test_replacement_close_keeps_the_rotation_record():
    """A direct replacement-style close must leave grace for the successor."""
    from subagents import cc_runtime_lifecycle as lifecycle
    from subagents.cc_runtime_state import _LIVE, _Live, session_recently_rotated

    class DrainedSession:
        mcp_config = None
        returncode = 0

        async def close(self):
            return self.returncode

    live = _Live(
        session=DrainedSession(),
        parent_thread_id=None,
        name="cc",
        tool_registry=object(),
        tool_token="replaced-secret",
    )
    _LIVE.put(_SESSION, live)

    assert await lifecycle.close_and_finalize(
        _SESSION, live, lambda *_args: None,
    ) is True

    assert _LIVE.get(_SESSION) is None
    assert session_recently_rotated(_SESSION) is True


def test_rotated_detail_wire_contract_matches_client_literal():
    """The standalone shim's mirrored literal must equal the canonical one."""
    from subagents.cc_runtime_state import TOOL_TOKEN_ROTATED_DETAIL
    from subagents.corsia_mcp_server import (
        TOOL_TOKEN_ROTATED_DETAIL as client_detail,
    )

    assert client_detail == TOOL_TOKEN_ROTATED_DETAIL


@pytest.mark.asyncio
async def test_client_retries_once_on_token_rotated_only(monkeypatch):
    from subagents import corsia_mcp_server

    monkeypatch.setattr(corsia_mcp_server, "_TOKEN_ROTATED_RETRY_DELAY_S", 0)
    requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                401, json={"detail": "Claude Code tool token rotated"},
            )
        return httpx.Response(200, text="recovered result")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"X-CORSIA-Tool-Token": "old-secret"},
    ) as client:
        result = await corsia_mcp_server._call_tool(
            client,
            f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}" + _BRIDGE_PATH,
            "read_file",
            {"path": "README.md"},
        )

    assert result == "recovered result"
    assert len(requests) == 2
    for req in requests:
        assert req["name"] == "read_file"
        assert req["arguments"] == {"path": "README.md"}
        assert "invocation_id" in req


@pytest.mark.asyncio
async def test_client_does_not_retry_other_denials(monkeypatch):
    from subagents import corsia_mcp_server

    monkeypatch.setattr(corsia_mcp_server, "_TOKEN_ROTATED_RETRY_DELAY_S", 0)
    counts = {"calls": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        counts["calls"] += 1
        return httpx.Response(
            401, json={"detail": "Invalid Claude Code tool token"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await corsia_mcp_server._call_tool(
                client,
                f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}" + _BRIDGE_PATH,
                "read_file",
                {"path": "README.md"},
            )

    assert counts["calls"] == 1


@pytest.mark.asyncio
async def test_client_retry_is_single_even_when_rotation_persists(monkeypatch):
    from subagents import corsia_mcp_server

    monkeypatch.setattr(corsia_mcp_server, "_TOKEN_ROTATED_RETRY_DELAY_S", 0)
    counts = {"calls": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        counts["calls"] += 1
        return httpx.Response(
            401, json={"detail": "Claude Code tool token rotated"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await corsia_mcp_server._call_tool(
                client,
                f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}" + _BRIDGE_PATH,
                "read_file",
                {"path": "README.md"},
            )

    assert counts["calls"] == 2
