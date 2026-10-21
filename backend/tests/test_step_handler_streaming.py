
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.step_handler import StepHandler, _attach_delivered_context_to_latest_assistant  # noqa: E402
from core.turn_core import TurnIdentity # noqa: E402


class _FakeBroadcast:
    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, payload: dict):
        self.events.append(payload)


def _bind_test_identity(handler: StepHandler) -> StepHandler:
    handler.bind_turn_identity(TurnIdentity("turn-test", 1))
    return handler


def test_generation_frames_carry_bound_turn_identity():
    bc = _FakeBroadcast()
    handler = StepHandler(
        thread_id="sess-identity",
        model_info={"model": "test/model", "model_name": "Test"},
        broadcast=bc,
        turn_id="turn-identity",
    )
    handler.bind_turn_identity(TurnIdentity("turn-identity", 17))

    asyncio.run(handler.handle("text_delta", {"content": "hello"}))

    assert bc.events == [{
        "type": "text_delta",
        "thread_id": "sess-identity",
        "content": "hello",
        "turn_id": "turn-identity",
        "turn_order": 17,
    }]


@pytest.fixture
def handler(monkeypatch):
    bc = _FakeBroadcast()
    h = _bind_test_identity(StepHandler(
        thread_id="sess-test",
        model_info={"model": "test/model", "model_name": "Test", "is_cloud": False},
        broadcast=bc,
        turn_id="turn-test",
    ))

    async def _stub_run_in_db(fn, *a, **kw):
        return None

    monkeypatch.setattr("core.step_handler.run_in_db", _stub_run_in_db)
    h._broadcast = bc  # convenience for assertions
    return h


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("text", {"content": "hello"}),
        (
            "tool_calls_start",
            {
                "tools": [{"id": "call-1", "name": "Read", "hint": "read", "args": "{}"}],
                "content": "",
                "tool_call_dicts": [],
            },
        ),
        ("tool_complete", {"id": "call-1", "name": "Read", "result": "done"}),
        ("error", {"message": "failed"}),
    ],
)
def test_persistence_failure_always_closes_pooled_connection(
    monkeypatch, event, payload
):
    class _TrackedDb:
        def __init__(self):
            self.closed = False
            self.rolled_back = False

        def commit(self):
            raise AssertionError("commit should not run after insert failure")

        def close(self):
            self.closed = True
            self.rolled_back = True

        def execute(self, sql, _params=()):
            """Answer the canonical writer's reads: one declared call, no result yet."""
            step = {"metadata": json.dumps({"turn_id": "turn-test", "llm_tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }]})} if "role='assistant'" in sql else None
            return SimpleNamespace(fetchone=lambda: step, fetchall=lambda: [])

    db = _TrackedDb()
    bc = _FakeBroadcast()
    h = _bind_test_identity(StepHandler(
        thread_id="sess-cleanup",
        model_info={"model": "test/model", "model_name": "Test"},
        broadcast=bc,
        turn_id="turn-test",
    ))
    if event == "tool_complete":
        h.tool_step_id = "assistant-step"
        h.tool_calls = [{"id": "call-1", "name": "Read", "result": None}]

    async def _run_in_db(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def _fail_insert(*args, **kwargs):
        raise sqlite3.OperationalError("forced persistence failure")

    from contextlib import contextmanager

    @contextmanager
    def _write_transaction(_connection, *, scope):
        try:
            yield db
        except BaseException:
            db.close()
            raise

    monkeypatch.setattr("core.step_handler.run_in_db", _run_in_db)
    monkeypatch.setattr("core.step_handler.get_db", lambda: db)
    monkeypatch.setattr("core.step_handler.write_transaction", _write_transaction)
    monkeypatch.setattr("core.step_handler.insert_message", _fail_insert)
    # A tool result is written by the canonical tool-result writer instead.
    monkeypatch.setattr("core.tool_result_delivery.insert_message", _fail_insert)

    with pytest.raises(sqlite3.OperationalError, match="forced persistence failure"):
        asyncio.run(h.handle(event, payload))

    assert db.closed is True
    assert db.rolled_back is True


def test_tool_start_dispatches_to_broadcast_only(handler):
    asyncio.run(handler.handle("tool_start", {
        "tool_id": "call_1",
        "name": "read_file",
        "hint": "",
        "args": '{"path": "x.py"}',
    }))

    bc = handler._broadcast
    assert len(bc.events) == 1
    ev = bc.events[0]
    assert ev["type"] == "tool_start"
    assert ev["tool_id"] == "call_1"
    assert ev["name"] == "read_file"
    assert ev["thread_id"] == "sess-test"
    assert handler.tool_calls == []
    assert handler.tool_hints == []


def test_tool_calls_start_skip_broadcast_still_populates_state(handler):
    payload = {
        "tools": [
            {"id": "call_1", "name": "read_file", "hint": "read x.py", "args": "{}"},
            {"id": "call_2", "name": "edit_file", "hint": "edit y.py", "args": "{}"},
        ],
        "content": "",
        "tool_call_dicts": [],
        "skip_broadcast": True,
    }
    asyncio.run(handler.handle("tool_calls_start", payload))

    bc = handler._broadcast
    assert all(e["type"] != "tool_start" for e in bc.events), bc.events

    assert [tc["id"] for tc in handler.tool_calls] == ["call_1", "call_2"]
    assert handler.tool_hints == ["read x.py", "edit y.py"]


def test_tool_calls_start_default_broadcasts_each_tool(handler):
    payload = {
        "tools": [
            {"id": "call_a", "name": "read_file", "hint": "h1", "args": ""},
            {"id": "call_b", "name": "edit_file", "hint": "h2", "args": ""},
        ],
        "content": "",
        "tool_call_dicts": [],
    }
    asyncio.run(handler.handle("tool_calls_start", payload))

    bc = handler._broadcast
    starts = [e for e in bc.events if e["type"] == "tool_start"]
    assert [e["tool_id"] for e in starts] == ["call_a", "call_b"]


def test_tool_step_persists_one_mixed_row_and_finalizes_with_linked_results(
    isolated_db,
):
    from core.turns_steps import USER_INTERRUPTION_TOOL_RESULT
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id, user_id, model, created_at, updated_at) "
        "VALUES ('sess-step', 'u-step', 'omlx@local/test', 't', 't')"
    )
    db.commit()
    db.close()

    bc = _FakeBroadcast()
    handler = _bind_test_identity(StepHandler(
        thread_id="sess-step",
        model_info={"model": "test/model", "model_name": "Test"},
        broadcast=bc,
        turn_id="turn-test",
    ))
    asyncio.run(handler.handle("tool_calls_start", {
        "tools": [
            {"id": "call-1", "name": "Read", "hint": "a", "args": "{}"},
            {"id": "call-2", "name": "Read", "hint": "b", "args": "{}"},
        ],
        "content": "I will inspect both.",
        "tool_call_dicts": [
            {"id": "call-1", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
            {"id": "call-2", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
        ],
    }))
    asyncio.run(handler.handle("tool_complete", {
        "id": "call-1", "name": "Read", "result": "real",
    }))
    asyncio.run(handler.handle("tool_complete", {
        "id": "call-2", "name": "Read", "result": USER_INTERRUPTION_TOOL_RESULT,
        "synthetic_tool_result": True,
        "synthetic_reason": "user_interruption",
    }))
    asyncio.run(handler.handle("tool_calls_done", {
        "tool_ids": ["call-1", "call-2"],
    }))

    db = get_db()
    rows = [dict(row) for row in db.execute(
        "SELECT id, role, content, metadata, created_at FROM messages "
        "WHERE thread_id='sess-step' ORDER BY rowid"
    ).fetchall()]
    db.close()
    assert [row["role"] for row in rows] == ["assistant", "tool", "tool"]
    # The Step id is random; each result id is derived from its invocation.
    assert [uuid.UUID(row["id"]).version for row in rows] == [4, 5, 5]
    assert rows[0]["content"] == "I will inspect both."
    assistant_step_id = rows[0]["id"]
    real_meta = json.loads(rows[1]["metadata"])
    cancelled_meta = json.loads(rows[2]["metadata"])
    assert real_meta["assistant_step_id"] == assistant_step_id
    assert real_meta["turn_id"] == "turn-test"
    assert real_meta["tool_invocation_id"] == "call-1"
    assert "is_error" not in real_meta
    assert cancelled_meta == {
        "tool_call_id": "call-2",
        "provider_tool_call_id": "call-2",
        "tool_invocation_id": "synthetic:user_interruption:call-2",
        "tool_result_binding_state": "bound",
        "assistant_step_id": assistant_step_id,
        "name": "Read",
        "arguments": {},
        "turn_id": "turn-test",
        "synthetic_tool_result": True,
        "synthetic_reason": "user_interruption",
        "tool_result_sha256": hashlib.sha256(
            USER_INTERRUPTION_TOOL_RESULT.encode("utf-8")
        ).hexdigest(),
        "tool_result_byte_count": len(
            USER_INTERRUPTION_TOOL_RESULT.encode("utf-8")
        ),
    }

    final = next(event for event in bc.events if event["type"] == "step_text")
    assert final["message_id"] == assistant_step_id
    assert final["content"] == "I will inspect both."
    assert isinstance(final["created_at"], str)
    assert final["finalized_tool_ids"] == ["call-1", "call-2"]
    assert [call["result"] for call in final["metadata"]["tool_calls"]] == [
        "real", USER_INTERRUPTION_TOOL_RESULT,
    ]


def test_turn_complete_projects_live_snapshot_without_widening_stored_receipt(monkeypatch):
    from infra.context_receipts import ContextReceiptIdentity

    bc = _FakeBroadcast()
    h = _bind_test_identity(StepHandler(
        thread_id="sess-test",
        model_info={"model": "test/model", "model_name": "Test", "is_cloud": False},
        broadcast=bc,
        turn_id="turn-test",
    ))
    h.step_ids.append("assistant-1")
    attach_calls = []

    async def fake_run_in_db(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "attach_delivered_context_to_latest_assistant":
            attach_calls.append((args, kwargs))
            return "assistant-1", {
                "model_context_window": 200_000,
                "total_used": 42,
                "token_usage_available": True,
                "token_source": "measured",
                "snapshot_id": "ctx-native-proof",
                "harness": "native",
                "catalog_model_id": "test/model",
                "runtime_context_epoch": 2,
            }
        return None

    monkeypatch.setattr("core.step_handler.run_in_db", fake_run_in_db)

    marker = "ROW25_NATIVE_TERMINAL_HEAVY_" + ("x" * 100_000)
    usage = {
        "snapshot_id": "ctx-native-proof",
        "model_context_window": 200_000,
        "total_used": 42,
        "token_usage_available": True,
        "token_source": "measured",
        "context_snapshot": {
            "snapshot_id": "ctx-native-proof",
            "source_inventory": [{"source_id": "native-system", "label": "System"}],
        },
        "source_artifacts": [{"payload": marker}],
    }
    identity = ContextReceiptIdentity("native", "test/model", 2)
    asyncio.run(h.handle("turn_complete", {
        "context_usage": usage,
        "_context_usage_identity": identity,
    }))

    assert attach_calls == [
        (
            (),
            {
                "thread_id": "sess-test",
                "step_ids": ["assistant-1"],
                "context_usage": usage,
                "identity": identity,
            },
        )
    ]
    assert bc.events[0]["type"] == "turn_complete"
    assert bc.events[0]["context_usage"] == {
        "model_context_window": 200_000,
        "total_used": 42,
        "token_usage_available": True,
        "token_source": "measured",
        "snapshot_id": "ctx-native-proof",
        "context_snapshot": {
            "snapshot_id": "ctx-native-proof",
            "source_inventory": [{"source_id": "native-system", "label": "System"}],
        },
    }
    assert bc.events[0]["context_message_id"] == "assistant-1"
    assert bc.events[0]["turn_id"] == "turn-test"
    assert bc.events[0]["turn_order"] == 1
    terminal_json = json.dumps(bc.events[0])
    assert marker not in terminal_json
    assert "source_artifacts" not in terminal_json
    assert bc.events[0]["context_usage"]["context_snapshot"] == {
        "snapshot_id": "ctx-native-proof",
        "source_inventory": [{"source_id": "native-system", "label": "System"}],
    }


def test_context_usage_boundary_broadcasts_compact_identity_receipt():
    from infra.context_receipts import ContextReceiptIdentity

    bc = _FakeBroadcast()
    handler = _bind_test_identity(StepHandler(
        thread_id="sess-test",
        model_info={"model": "test/model", "model_name": "Test"},
        broadcast=bc,
        turn_id="turn-test",
    ))
    marker = "SAFE_BOUNDARY_HEAVY_" + ("x" * 100_000)
    usage = {
        "model_context_window": 200_000,
        "total_used": 42,
        "token_usage_available": True,
        "token_source": "measured",
        "context_snapshot": {
            "snapshot_id": "ctx-native-boundary",
            "source_inventory": [{"source_id": "native-boundary", "label": "System"}],
            "payload": marker,
        },
    }

    asyncio.run(handler.handle("context_usage", {
        "context_usage": usage,
        "_context_usage_identity": ContextReceiptIdentity(
            "native", "test/model", 2,
        ),
    }))

    assert bc.events == [{
        "type": "context_usage_updated",
        "thread_id": "sess-test",
        "context_usage": {
            "model_context_window": 200_000,
            "total_used": 42,
            "token_usage_available": True,
            "token_source": "measured",
            "context_snapshot": {
                "snapshot_id": "ctx-native-boundary",
                "source_inventory": [{"source_id": "native-boundary", "label": "System"}],
            },
        },
        "turn_id": "turn-test",
        "turn_order": 1,
    }]
    assert marker not in json.dumps(bc.events)


def test_context_usage_boundary_rejects_malformed_internal_frame():
    handler = _bind_test_identity(StepHandler(
        thread_id="sess-test",
        model_info={"model": "test/model", "model_name": "Test"},
        broadcast=_FakeBroadcast(),
        turn_id="turn-test",
    ))

    with pytest.raises(
        ValueError,
        match="requires usage and exact provider identity",
    ):
        asyncio.run(handler.handle("context_usage", {
            "context_usage": {"total_used": 42},
        }))


def test_turn_complete_broadcast_survives_receipt_attachment_failure(monkeypatch):
    from infra.context_receipts import ContextReceiptIdentity
    from infra.context_receipts import live_context_usage_payload

    bc = _FakeBroadcast()
    h = _bind_test_identity(StepHandler(
        thread_id="sess-test",
        model_info={"model": "test/model", "model_name": "Test", "is_cloud": False},
        broadcast=bc,
        turn_id="turn-test",
    ))
    h.step_ids.append("assistant-1")

    async def fake_run_in_db(fn, *args, **kwargs):
        raise RuntimeError("message update unavailable")

    monkeypatch.setattr("core.step_handler.run_in_db", fake_run_in_db)

    marker = "ROW25_NATIVE_FAILURE_HEAVY_" + ("x" * 100_000)
    usage = {
        "snapshot_id": "ctx-native-proof",
        "total_used": 42,
        "context_snapshot": {
            "snapshot_id": "ctx-native-proof",
            "frozen_prompt_material": marker,
        },
        "source_artifacts": [{"payload": marker}],
    }
    asyncio.run(h.handle("turn_complete", {
        "context_usage": usage,
        "_context_usage_identity": ContextReceiptIdentity(
            "native", "test/model", 2,
        ),
    }))

    assert bc.events[0]["type"] == "turn_complete"
    assert bc.events[0]["context_usage"] == live_context_usage_payload(usage)
    assert "context_message_id" not in bc.events[0]
    assert marker not in json.dumps(bc.events[0])


def test_turn_complete_without_an_assistant_row_uses_the_compact_summary(monkeypatch):
    from infra.context_receipts import ContextReceiptIdentity, live_context_usage_payload

    bc = _FakeBroadcast()
    h = _bind_test_identity(StepHandler(
        thread_id="sess-test",
        model_info={"model": "test/model", "model_name": "Test", "is_cloud": False},
        broadcast=bc,
        turn_id="turn-test",
    ))
    h.step_ids.append("missing-assistant")

    async def fake_run_in_db(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "attach_delivered_context_to_latest_assistant":
            return None
        return None

    monkeypatch.setattr("core.step_handler.run_in_db", fake_run_in_db)

    usage = {
        "snapshot_id": "ctx-native-proof",
        "context_snapshot": {"snapshot_id": "ctx-native-proof"},
    }
    asyncio.run(h.handle("turn_complete", {
        "context_usage": usage,
        "_context_usage_identity": ContextReceiptIdentity(
            "native", "test/model", 2,
        ),
    }))

    assert bc.events[0]["context_usage"] == live_context_usage_payload(usage)
    assert "context_message_id" not in bc.events[0]


def test_attach_delivered_context_updates_latest_assistant_metadata(monkeypatch):
    from infra.context_receipts import ContextReceiptIdentity

    class _NoClose(sqlite3.Connection):
        def close(self):
            pass

    conn = sqlite3.connect(":memory:", factory=_NoClose)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            metadata TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "assistant-1",
            "sess-test",
            "assistant",
            "done",
            json.dumps({"model": "test/model", "turn_id": "turn-test"}),
            "2026-07-02T00:00:00Z",
        ),
    )

    conn.commit()
    monkeypatch.setattr("infra.database.get_db", lambda: conn)

    usage = {
        "snapshot_id": "ctx-native-proof",
        "context_snapshot": {
            "snapshot_id": "ctx-native-proof",
            "source_inventory": [{"source_id": "role:agent-owner-map"}],
        },
        "source_artifacts": [{"kind": "provider-bound-payload"}],
    }

    attached = _attach_delivered_context_to_latest_assistant(
        thread_id="sess-test",
        step_ids=["assistant-1"],
        context_usage=usage,
        identity=ContextReceiptIdentity("native", "test/model", 2),
    )
    assert attached is not None
    assert attached[0] == "assistant-1"

    row = conn.execute(
        "SELECT metadata FROM messages WHERE id = ?",
        ("assistant-1",),
    ).fetchone()
    metadata = json.loads(row["metadata"])
    assert metadata["context_usage"] == {
        "snapshot_id": "ctx-native-proof",
        "harness": "native",
        "catalog_model_id": "test/model",
        "runtime_context_epoch": 2,
    }
    assert "context_snapshot" not in metadata["context_usage"]


def test_unknown_step_type_raises(handler):
    with pytest.raises(ValueError, match="Unhandled step type 'totally_unknown'"):
        asyncio.run(handler.handle("totally_unknown", {}))


def test_turn_complete_frame_carries_the_looked_up_rows(monkeypatch):
    bc = _FakeBroadcast()
    h = _bind_test_identity(StepHandler(
        thread_id="sess-test",
        model_info={"model": "test/model", "model_name": "Test", "is_cloud": False},
        broadcast=bc,
        turn_id="turn-test",
    ))

    async def no_receipt(fn, *args, **kwargs):
        return None

    monkeypatch.setattr("core.step_handler.run_in_db", no_receipt)
    looked_up = [{"server": "github", "display_name": "GitHub", "tokens": 1_200}]

    asyncio.run(h.handle("turn_complete", {
        "context_usage": {
            "model_context_window": 200_000, "total_used": 42, "loaded_tool_tokens": looked_up,
        },
    }))

    assert bc.events[0]["context_usage"]["loaded_tool_tokens"] == looked_up
