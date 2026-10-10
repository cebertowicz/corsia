from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import pytest


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            seq INTEGER
        );
        CREATE TABLE active_turns (
            turn_order INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            thread_id TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );
        """
    )
    return connection


def test_terminal_receipt_is_compact_and_exact():
    from infra.context_receipts import (
        ContextReceiptIdentity,
        attach_context_receipt,
    )

    connection = _connection()
    connection.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES ('assistant-1','thread-1','assistant','answer',?,'now',1)",
        (json.dumps({"turn_id": "turn-1", "reasoning": "keep"}),),
    )
    marker = "full-payload-marker-" + ("x" * 100_000)
    full_usage = {
        "model_context_window": 262_144,
        "total_used": 120_000,
        "utilization": 0.458,
        "token_usage_available": True,
        "token_source": "measured",
        "snapshot_id": "ctx-1",
        "continuity_mode": "checkpoint_tail",
        "context_snapshot": {"frozen_prompt_material": marker},
        "source_artifacts": [{"payload": marker}],
    }

    receipt = attach_context_receipt(
        "thread-1",
        "assistant-1",
        context_usage=full_usage,
        identity=ContextReceiptIdentity(
            "codex",
            "codex/luna",
            3,
            "a" * 64,
        ),
        db=connection,
    )

    stored = json.loads(
        connection.execute(
            "SELECT metadata FROM messages WHERE id='assistant-1'"
        ).fetchone()["metadata"]
    )
    assert receipt == stored["context_usage"]
    assert stored["reasoning"] == "keep"
    assert receipt == {
        "model_context_window": 262_144,
        "total_used": 120_000,
        "utilization": 0.458,
        "token_usage_available": True,
        "token_source": "measured",
        "snapshot_id": "ctx-1",
        "continuity_mode": "checkpoint_tail",
        "harness": "codex",
        "catalog_model_id": "codex/luna",
        "runtime_context_epoch": 3,
        "producer_handle_fingerprint": "a" * 64,
    }
    serialized = json.dumps(stored)
    assert marker not in serialized
    assert "context_snapshot" not in receipt
    assert "source_artifacts" not in receipt


def _insert_terminal(
    connection: sqlite3.Connection,
    turn_id: str,
    *,
    status: str = "completed",
    message_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO active_turns "
        "(id,thread_id,status,started_at,ended_at) VALUES (?,?,?,'start','end')",
        (turn_id, "thread-1", status),
    )
    if message_id is not None:
        order = connection.execute(
            "SELECT turn_order FROM active_turns WHERE id=?", (turn_id,)
        ).fetchone()["turn_order"]
        connection.execute(
            "INSERT INTO messages "
            "(id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES (?,?,'assistant','answer',?,'now',?)",
            (message_id, "thread-1", json.dumps({"turn_id": turn_id}), order),
        )


def _measured_usage(total: int = 120_000) -> dict:
    return {
        "model_context_window": 262_144,
        "total_used": total,
        "utilization": round(total / 262_144, 3),
        "token_usage_available": True,
        "token_source": "measured",
    }


def _codex_identity(**changes):
    from infra.context_receipts import ContextReceiptIdentity

    values = {
        "harness": "codex",
        "catalog_model_id": "codex/luna",
        "runtime_context_epoch": 3,
        "producer_handle_fingerprint": "a" * 64,
        **changes,
    }
    return ContextReceiptIdentity(**values)


def test_latest_measured_receipt_belongs_to_latest_terminal_turn():
    from infra.context_receipts import (
        attach_context_receipt,
        latest_measured_context_receipt,
    )

    connection = _connection()
    _insert_terminal(connection, "turn-1", message_id="assistant-1")
    _insert_terminal(connection, "turn-2", message_id="assistant-2")
    attach_context_receipt(
        "thread-1",
        "assistant-1",
        context_usage=_measured_usage(80_000),
        identity=_codex_identity(),
        db=connection,
    )
    attach_context_receipt(
        "thread-1",
        "assistant-2",
        context_usage=_measured_usage(120_000),
        identity=_codex_identity(),
        db=connection,
    )

    assert latest_measured_context_receipt(
        "thread-1", _codex_identity(), db=connection
    ) == {
        "message_id": "assistant-2",
        "context_usage": {
            **_measured_usage(120_000),
            "harness": "codex",
            "catalog_model_id": "codex/luna",
            "runtime_context_epoch": 3,
            "producer_handle_fingerprint": "a" * 64,
        },
    }


def test_newer_cancelled_turn_fences_an_older_measurement():
    from infra.context_receipts import (
        attach_context_receipt,
        latest_measured_context_receipt,
    )

    connection = _connection()
    _insert_terminal(connection, "turn-1", message_id="assistant-1")
    attach_context_receipt(
        "thread-1",
        "assistant-1",
        context_usage=_measured_usage(),
        identity=_codex_identity(),
        db=connection,
    )
    _insert_terminal(connection, "turn-2", status="cancelled")

    assert latest_measured_context_receipt(
        "thread-1", _codex_identity(), db=connection
    ) is None


def test_newer_unavailable_receipt_fences_an_older_measurement():
    from infra.context_receipts import (
        attach_context_receipt,
        latest_measured_context_receipt,
    )

    connection = _connection()
    _insert_terminal(connection, "turn-1", message_id="assistant-1")
    _insert_terminal(connection, "turn-2", message_id="assistant-2")
    attach_context_receipt(
        "thread-1",
        "assistant-1",
        context_usage=_measured_usage(),
        identity=_codex_identity(),
        db=connection,
    )
    attach_context_receipt(
        "thread-1",
        "assistant-2",
        context_usage={
            "model_context_window": 262_144,
            "total_used": None,
            "utilization": None,
            "token_usage_available": False,
            "stale_reason": "terminal_without_measurement",
        },
        identity=_codex_identity(),
        db=connection,
    )

    assert latest_measured_context_receipt(
        "thread-1", _codex_identity(), db=connection
    ) is None


def test_latest_measured_receipt_requires_exact_provider_identity():
    from infra.context_receipts import (
        attach_context_receipt,
        latest_measured_context_receipt,
    )

    connection = _connection()
    _insert_terminal(connection, "turn-1", message_id="assistant-1")
    attach_context_receipt(
        "thread-1",
        "assistant-1",
        context_usage=_measured_usage(),
        identity=_codex_identity(),
        db=connection,
    )

    assert latest_measured_context_receipt(
        "thread-1",
        _codex_identity(catalog_model_id="codex/other"),
        db=connection,
    ) is None
    assert latest_measured_context_receipt(
        "thread-1",
        _codex_identity(runtime_context_epoch=4),
        db=connection,
    ) is None
    assert latest_measured_context_receipt(
        "thread-1",
        _codex_identity(producer_handle_fingerprint="b" * 64),
        db=connection,
    ) is None


def test_producer_handle_fingerprint_is_canonical_and_opaque():
    from infra.context_receipts import producer_handle_fingerprint

    left = producer_handle_fingerprint({"thread": "t", "pid": 7})
    reordered = producer_handle_fingerprint({"pid": 7, "thread": "t"})
    changed = producer_handle_fingerprint({"thread": "t", "pid": 8})

    assert left == reordered
    assert len(left) == 64
    assert set(left) <= set("0123456789abcdef")
    assert changed != left


def test_current_receipt_identity_stamps_native_model_and_epoch():
    from infra.context_receipts import current_context_receipt_identity

    assert current_context_receipt_identity(
        "thread-1",
        harness="native",
        catalog_model_id="omlx@test/model",
        runtime_context_epoch=8,
    ) == _native_identity("omlx@test/model", 8)


def _native_identity(model: str, epoch: int):
    from infra.context_receipts import ContextReceiptIdentity

    return ContextReceiptIdentity("native", model, epoch)


@pytest.mark.parametrize(
    ("harness", "runtime_module"),
    [
        ("claude-code", "subagents.cc_runtime"),
        ("codex", "subagents.codex_runtime_state"),
    ],
)
def test_current_receipt_identity_requires_exact_live_external_handle(
    harness,
    runtime_module,
    monkeypatch,
):
    import importlib

    from infra.context_receipts import (
        current_context_receipt_identity,
        producer_handle_fingerprint,
    )

    @dataclass(frozen=True)
    class FrozenHandle:
        catalog_model_id: str
        runtime_context_epoch: int
        provider_handle: str

    model = f"{harness}/test"
    handle = FrozenHandle(model, 12, "provider-handle-1")
    runtime = importlib.import_module(runtime_module)
    monkeypatch.setattr(
        runtime,
        "trusted_handle_fingerprint",
        lambda _thread_id: handle,
    )

    identity = current_context_receipt_identity(
        "thread-1",
        harness=harness,
        catalog_model_id=model,
        runtime_context_epoch=12,
    )
    assert identity is not None
    assert identity.producer_handle_fingerprint == producer_handle_fingerprint(handle)
    assert current_context_receipt_identity(
        "thread-1",
        harness=harness,
        catalog_model_id=f"{model}-other",
        runtime_context_epoch=12,
    ) is None


def test_unavailable_usage_and_boundary_usage_keep_runtime_ctx_shape():
    from infra.context_receipts import (
        context_usage_for_boundary,
        unavailable_context_usage,
    )

    unavailable = unavailable_context_usage(
        model_context_window=262_144,
        stale_reason="terminal_without_measurement",
    )
    assert unavailable["total_used"] is None
    assert unavailable["model_context_window"] == 262_144
    assert unavailable["token_usage_available"] is False
    snapshot = {"snapshot_id": "ctx-1", "content_hash": "c" * 64}
    usage = context_usage_for_boundary(
        snapshot=snapshot,
        source_artifacts=[{"harness": "native"}],
        continuity_mode="checkpoint_tail",
        extra={"total_used": 10},
    )
    assert usage == {
        "snapshot_id": "ctx-1",
        "context_snapshot": snapshot,
        "continuity_mode": "checkpoint_tail",
        "source_artifacts": [{"harness": "native"}],
        "content_hash": "c" * 64,
        "total_used": 10,
    }
