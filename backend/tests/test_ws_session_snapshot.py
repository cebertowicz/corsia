
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("CORSIA_WORKSPACE", "/tmp/corsia-test-ws")

from infra import state  # noqa: E402
from core.turn_core import TurnIdentity  # noqa: E402
from infra.database import get_db  # noqa: E402
from infra.state import turn_buffers as stb  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    stb._reset_for_tests()
    state._active_turns.clear()
    state._ws_thread_clients.clear()
    yield
    stb._reset_for_tests()
    state._active_turns.clear()
    state._ws_thread_clients.clear()


class _FakeWS:

    def __init__(self):
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)


def _ws_target(model: str, db=None):
    from llm.resolution import ExecutionTarget

    if model.startswith("claude-code/"):
        harness, backend = "claude-code", "cc"
    elif model.startswith("codex/"):
        harness, backend = "codex", "codex"
    else:
        harness, backend = "native", "api"
    return ExecutionTarget(
        catalog_id=model,
        harness=harness,
        provider=harness,
        backend=backend,
        transport_model=model,
        expected_reported_model=None,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name=model,
        supports_reasoning=False,
    )


def _persist_active_generation(
    thread_id: str,
    user_id: str,
    turn_id: str,
) -> None:
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
            "VALUES (?, ?, 'oMLX/qwen', 't', 't')",
            (thread_id, user_id),
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id, thread_id, execution_epoch, model, status, started_at, boot_epoch) "
            "VALUES (?, ?, 0, 'oMLX/qwen', 'running', 't', 'test-boot')",
            (turn_id, thread_id),
        )
        db.commit()
    finally:
        db.close()


async def _subscribe(ws: _FakeWS, thread_id: str) -> None:
    from ws import _handle_subscribe
    from unittest.mock import patch

    async def _fake_owned_thread(_user, _thread_id):
        return {
            "model": "oMLX/qwen",
            "parent_thread_id": None,
            "title": None,
            "spawned_by": None,
        }

    with patch("ws._owned_thread", _fake_owned_thread):
        await _handle_subscribe(ws, {"id": "user-1"}, thread_id)


@pytest.mark.asyncio
async def test_snapshot_envelope_marks_active_turn(monkeypatch):
    monkeypatch.setattr(
        "ws.thread_control.active_turns_for_owner",
        lambda *_args, **_kwargs: [
            {
                "thread_id": "sess-A",
                "turn_id": "turn-A",
                "turn_order": 1,
            },
        ],
    )
    identity = TurnIdentity("turn-A", 1)
    stb.init("sess-A", identity)
    stb.append_reasoning("sess-A", identity, "thinking...")

    ws = _FakeWS()
    await _subscribe(ws, "sess-A")

    assert len(ws.sent) == 1
    msg = ws.sent[0]
    assert msg["type"] == "session_snapshot"
    assert msg["thread_id"] == "sess-A"
    assert msg["turn_id"] == "turn-A"
    assert msg["turn_order"] == 1
    assert msg["state"] is not None
    assert msg["state"]["reasoning"] == "thinking..."


@pytest.mark.asyncio
async def test_snapshot_envelope_marks_inactive_when_buffer_cleared(monkeypatch):
    monkeypatch.setattr(
        "ws.thread_control.active_turns_for_owner",
        lambda *_args, **_kwargs: [],
    )
    ws = _FakeWS()
    await _subscribe(ws, "sess-B")

    msg = ws.sent[0]
    assert msg["type"] == "session_snapshot"
    assert msg["state"] is None
    assert msg["turn_id"] is None


@pytest.mark.asyncio
async def test_snapshot_envelope_active_turn_with_no_buffer_yet(monkeypatch):
    monkeypatch.setattr(
        "ws.thread_control.active_turns_for_owner",
        lambda *_args, **_kwargs: [
            {
                "thread_id": "sess-C",
                "turn_id": "turn-C",
                "turn_order": 3,
                "visible_root_thread_id": "sess-C",
                "started_at": "2026-09-26T18:00:00+00:00",
            },
        ],
    )

    ws = _FakeWS()
    await _subscribe(ws, "sess-C")

    msg = ws.sent[0]
    assert msg["state"] is None
    assert msg["turn_id"] == "turn-C"
    assert msg["turn_order"] == 3
    # The subscribe snapshot carries the same visible-root contract as the
    # auth snapshot, so a client that adopts the Turn here learns its lineage.
    assert msg["visible_root_thread_id"] == "sess-C"
    # And its durable start, so a client subscribing mid-Turn times it from
    # the same instant a live `turn_activity` frame names.
    assert msg["started_at"] == "2026-09-26T18:00:00+00:00"


@pytest.mark.asyncio
async def test_auth_ok_active_turns_uses_owner_scoped_generations(monkeypatch):
    from ws import thread_control

    monkeypatch.setattr(
        thread_control,
        "active_turns_for_owner",
        lambda _user_id: [
            {"thread_id": "native", "turn_id": "turn-native", "turn_order": 1},
            {"thread_id": "cc", "turn_id": "turn-cc", "turn_order": 2},
            {"thread_id": "codex", "turn_id": "turn-codex", "turn_order": 3},
        ],
    )
    monkeypatch.setattr(
        thread_control, "authenticate",
        lambda tok: {"id": "u-1", "name": "u", "profiles": []})

    class _AuthWS(_FakeWS):
        async def receive_json(self):
            return {"token": "t"}

    ws = _AuthWS()
    user = await thread_control.authenticate_ws(ws)
    assert user is not None

    auth_ok = next(m for m in ws.sent if m["type"] == "auth_ok")
    assert auth_ok["active_turns"] == [
        {"thread_id": "native", "turn_id": "turn-native", "turn_order": 1},
        {"thread_id": "cc", "turn_id": "turn-cc", "turn_order": 2},
        {"thread_id": "codex", "turn_id": "turn-codex", "turn_order": 3},
    ]
    state._ws_user_clients.pop("u-1", None)


@pytest.mark.asyncio
async def test_auth_restoration_exposes_only_owner_active_generations(
    isolated_db,
    monkeypatch,
):
    from ws import thread_control

    _persist_active_generation("session-A", "user-A", "turn-A")
    _persist_active_generation("session-B", "user-B", "turn-B")
    monkeypatch.setattr(
        thread_control,
        "authenticate",
        lambda _tok: {"id": "user-A", "name": "A", "profiles": []},
    )

    class _AuthWS(_FakeWS):
        async def receive_json(self):
            return {"token": "t"}

    ws = _AuthWS()
    user = await thread_control.authenticate_ws(ws)
    assert user is not None

    auth_ok = next(message for message in ws.sent if message["type"] == "auth_ok")
    assert auth_ok["active_turns"] == [
        {"thread_id": "session-A", "turn_id": "turn-A", "turn_order": 1,
         "visible_root_thread_id": "session-A", "started_at": "t"},
    ]
    assert "active_sessions" not in auth_ok
    state._ws_user_clients.pop("user-A", None)


@pytest.mark.asyncio
async def test_snapshot_never_pairs_active_turn_with_another_generation_buffer(
    isolated_db,
):
    from ws import thread_control

    _persist_active_generation("session-A", "user-A", "turn-A")
    identity = TurnIdentity("turn-B", 2)
    stb.init("session-A", identity)
    stb.append_content("session-A", identity, "B output")
    ws = _FakeWS()

    async def _owned(_user, _thread_id):
        return {
            "model": "oMLX/qwen",
            "parent_thread_id": None,
            "title": None,
            "spawned_by": None,
        }

    await thread_control.handle_subscribe(
        ws,
        {"id": "user-A"},
        "session-A",
        owned_thread_fn=_owned,
    )

    snapshot = next(
        message for message in ws.sent if message["type"] == "session_snapshot"
    )
    assert snapshot["turn_id"] == "turn-A"
    assert snapshot["turn_order"] == 1
    assert snapshot["state"] is None


@pytest.mark.asyncio
async def test_public_active_turn_fallback_strips_private_identity(
    isolated_db,
    monkeypatch,
):
    from api import thread_runtime

    _persist_active_generation("session-http", "user-A", "turn-http")
    identity = TurnIdentity("turn-http", 1)
    stb.init("session-http", identity)
    stb.append_content("session-http", identity, "partial")
    monkeypatch.setattr(
        thread_runtime,
        "authenticate",
        lambda _token: {"id": "user-A"},
    )

    result = await thread_runtime.active_turn_state("session-http", token="t")

    assert result["active"] is True
    assert result["state"]["content"] == "partial"
    assert "turn_id" not in result["state"]
    assert "turn_order" not in result["state"]


@pytest.mark.asyncio
async def test_subscribe_emits_subagent_meta_for_cc_child():
    from ws import _handle_subscribe
    from unittest.mock import patch

    row = {"model": "claude-code/sonnet",
           "parent_thread_id": "parent-1", "title": "My Subagent",
           "spawned_by": "parent-1"}

    async def _fake_owned_thread(_user, _thread_id):
        return row

    ws = _FakeWS()
    with patch("ws._owned_thread", _fake_owned_thread), \
         patch("infra.execution_class.resolve_execution_target", _ws_target), \
         patch("ws.thread_control.active_turns_for_owner", return_value=[]), \
         patch("subagents.cc_runtime.is_live", lambda sid: True):
        await _handle_subscribe(ws, {"id": "user-1"}, "child-1")

    metas = [m for m in ws.sent if m["type"] == "subagent_meta"]
    assert len(metas) == 1
    meta = metas[0]
    assert meta["thread_id"] == "child-1"
    assert meta["parent_thread_id"] == "parent-1"
    assert meta["name"] == "My Subagent"
    assert meta["provider"] == "cc"
    assert meta["live"] is False


@pytest.mark.asyncio
async def test_subscribe_emits_subagent_meta_for_codex_child():
    from ws import _handle_subscribe
    from unittest.mock import patch

    row = {"model": "codex/gpt-5", "parent_thread_id": "parent-2",
           "title": "Codex Worker", "spawned_by": "parent-2"}

    async def _fake_owned_thread(_user, _thread_id):
        return row

    ws = _FakeWS()
    with patch("ws._owned_thread", _fake_owned_thread), \
         patch("infra.execution_class.resolve_execution_target", _ws_target), \
         patch(
             "ws.thread_control.active_turns_for_owner",
             return_value=[
                 {
                     "thread_id": "codex-child-1",
                     "turn_id": "turn-codex",
                     "turn_order": 1,
                 },
             ],
         ), \
         patch("subagents.codex_runtime.is_live", lambda sid: False):
        await _handle_subscribe(ws, {"id": "user-1"}, "codex-child-1")

    metas = [m for m in ws.sent if m["type"] == "subagent_meta"]
    assert len(metas) == 1
    meta = metas[0]
    assert meta["provider"] == "codex"
    assert meta["live"] is True


@pytest.mark.asyncio
async def test_subscribe_emits_subagent_meta_for_native_child():
    """Native/oMLX delegated subs must emit subagent_meta with provider='native'.

    Regression guard for #551: before the fix, the else branch returned early
    so native subs never received subagent_meta and were mislabeled as CC on
    the frontend.
    """
    from ws import _handle_subscribe
    from unittest.mock import patch

    for model in ("omlx@server/Qwen-35B", "anthropic/claude-3-5-sonnet"):
        row = {"model": model, "parent_thread_id": "parent-3",
               "title": "Native Worker", "spawned_by": "parent-3"}

        async def _fake_owned_thread(_user, _thread_id):
            return row

        ws = _FakeWS()
        with patch("ws._owned_thread", _fake_owned_thread), \
             patch("infra.execution_class.resolve_execution_target", _ws_target):
            await _handle_subscribe(ws, {"id": "user-1"}, "native-child-1")

        metas = [m for m in ws.sent if m["type"] == "subagent_meta"]
        assert len(metas) == 1, f"expected subagent_meta for native model {model!r}"
        meta = metas[0]
        assert meta["provider"] == "native", f"wrong provider for {model!r}: {meta['provider']}"
        assert meta["live"] is False
        assert meta["model"] == model


@pytest.mark.asyncio
async def test_subscribe_keeps_removed_model_subagent_connected():
    from llm.resolution import ModelUnavailableError
    from ws import _handle_subscribe
    from unittest.mock import patch

    removed_model = "omlx@local/qwen-flash-next"
    row = {
        "model": removed_model,
        "parent_thread_id": "parent-stale",
        "title": "Stale worker",
        "spawned_by": "parent-stale",
    }

    async def _fake_owned_thread(_user, _thread_id):
        return row

    def missing_model(model):
        raise ModelUnavailableError(model, f"Model '{model}' not found in catalog")

    ws = _FakeWS()
    with patch("ws._owned_thread", _fake_owned_thread), \
         patch("infra.execution_class.resolve_execution_target", missing_model), \
         patch("ws.thread_control.active_turns_for_owner", return_value=[]):
        try:
            await _handle_subscribe(ws, {"id": "user-1"}, "stale-child")
        except ModelUnavailableError as exc:
            pytest.fail(f"subscription raised for a removed model: {exc}")

    assert ws in state._ws_thread_clients["stale-child"]
    assert [message["type"] for message in ws.sent] == [
        "session_snapshot", "subagent_meta",
    ]
    meta = ws.sent[-1]
    assert meta["provider"] == "unavailable"
    assert meta["model"] == removed_model


@pytest.mark.asyncio
async def test_subscribe_subagent_meta_includes_model_for_cc():
    """subagent_meta must include the session model so the frontend can set it
    without waiting for a separate subagent_init event."""
    from ws import _handle_subscribe
    from unittest.mock import patch

    row = {"model": "claude-code/opus", "parent_thread_id": "parent-4",
           "title": "Opus Sub", "spawned_by": "parent-4"}

    async def _fake_owned_thread(_user, _thread_id):
        return row

    ws = _FakeWS()
    with patch("ws._owned_thread", _fake_owned_thread), \
         patch("infra.execution_class.resolve_execution_target", _ws_target), \
         patch("subagents.cc_runtime.is_live", lambda sid: False):
        await _handle_subscribe(ws, {"id": "user-1"}, "cc-child-model")

    meta = next(m for m in ws.sent if m["type"] == "subagent_meta")
    assert meta["model"] == "claude-code/opus"


@pytest.mark.asyncio
async def test_subscribe_no_subagent_meta_for_attached_main():
    from ws import _handle_subscribe
    from unittest.mock import patch

    row = {"model": "claude-code/opus",
           "parent_thread_id": "parent-1", "title": "Attached conductor",
           "spawned_by": None}

    async def _fake_owned_thread(_user, _thread_id):
        return row

    ws = _FakeWS()
    with patch("ws._owned_thread", _fake_owned_thread), \
         patch("subagents.cc_runtime.is_live", lambda sid: True):
        await _handle_subscribe(ws, {"id": "user-1"}, "att-1")

    assert not any(m["type"] == "subagent_meta" for m in ws.sent)
    assert any(m["type"] == "session_snapshot" for m in ws.sent)


@pytest.mark.asyncio
async def test_subscribe_emits_subagent_meta_for_native_child():
    """Native subagent threads (model not cc/codex) must also receive subagent_meta."""
    from ws import _handle_subscribe
    from unittest.mock import patch

    row = {"model": "oMLX/qwen3-35b", "parent_thread_id": None,
           "title": "Native Worker", "spawned_by": "native-parent-1"}

    async def _fake_owned_thread(_user, _thread_id):
        return row

    ws = _FakeWS()
    with patch("ws._owned_thread", _fake_owned_thread), \
         patch("infra.execution_class.resolve_execution_target", _ws_target):
        await _handle_subscribe(ws, {"id": "user-1"}, "native-child-1")

    metas = [m for m in ws.sent if m["type"] == "subagent_meta"]
    assert len(metas) == 1
    meta = metas[0]
    assert meta["thread_id"] == "native-child-1"
    assert meta["parent_thread_id"] == "native-parent-1"
    assert meta["provider"] == "native"
    assert meta["live"] is False


@pytest.mark.asyncio
async def test_subscribe_uses_spawned_by_for_parent_id_when_parent_session_id_null():
    """spawned_by is the canonical parent field; parent_thread_id may be null."""
    from ws import _handle_subscribe
    from unittest.mock import patch

    row = {"model": "claude-code/sonnet", "parent_thread_id": None,
           "title": "Worker", "spawned_by": "real-parent"}

    async def _fake_owned_thread(_user, _thread_id):
        return row

    ws = _FakeWS()
    with patch("ws._owned_thread", _fake_owned_thread), \
         patch("infra.execution_class.resolve_execution_target", _ws_target), \
         patch("subagents.cc_runtime.is_live", lambda sid: False):
        await _handle_subscribe(ws, {"id": "user-1"}, "w-1")

    metas = [m for m in ws.sent if m["type"] == "subagent_meta"]
    assert len(metas) == 1
    assert metas[0]["parent_thread_id"] == "real-parent"
