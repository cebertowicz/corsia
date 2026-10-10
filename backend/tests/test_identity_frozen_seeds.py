"""Persisted identity seeds keep their exact bytes.

Each id below is a uuid5 derived from a seed string. Rows are found again, or
deduplicated, by re-deriving the id, so a seed that changes without a data
migration orphans or duplicates them. Schema v79 moved the two re-derived
tables from the former product name to these seeds. The expected ids are
spelled here as the whole derivation string, independently of the constants.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from automation import managed_process_events, restart_wakes, restart_webhooks
from infra import delegation_markers, product_events
from registry import registration_events
from subagents import cc_transcript_projection

def _uuid5(derivation: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, derivation))


EXPECTED = {
    "restart_registration_a": _uuid5("corsia-restart:external:thread-1:turn-1"),
    "restart_registration_b": _uuid5("corsia-restart:op-9:thread-2:turn-2"),
    "restart_input_a": _uuid5("corsia-restart-input:restart_resume:reg-1"),
    "restart_input_b": _uuid5("corsia-restart-input:restart_recovery:reg-2"),
    "dead_letter_a": _uuid5("corsia-webhook-dead-letter:delivery-1"),
    "dead_letter_b": _uuid5("corsia-webhook-dead-letter:delivery-2"),
    "send_ref_a": _uuid5("corsia-send-ref:sender-1:target-1:input-1"),
    "send_ref_b": _uuid5("corsia-send-ref:sender-1:target-1:input-2"),
    "registration_event_a": _uuid5("corsia-artifact-registration:art-1:registered:cause-1"),
    "registration_event_b": _uuid5("corsia-artifact-registration:art-2:rehomed:cause-2"),
    "cc_transcript_a": _uuid5("corsia:claude-code:reconstructed-transcript:sess-1:msg-1:user"),
    "cc_transcript_b": _uuid5(
        "corsia:claude-code:reconstructed-transcript:sess-2:msg-2:assistant"
    ),
}


class _FakeConnection:
    def __init__(self, rowcount: int = 1) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rowcount = rowcount

    def execute(self, sql: str, params: tuple = ()):
        self.calls.append((sql, params))
        return SimpleNamespace(rowcount=self.rowcount, fetchone=lambda: (1,))


def _fake_write_transaction(connection):
    @contextlib.contextmanager
    def _factory(*_args, **_kwargs):
        yield connection

    return _factory


def _restart_input_id(monkeypatch, kind: str, registration_id: str) -> str:
    captured: dict = {}
    monkeypatch.setattr(
        "events.delivery.enqueue_event_wake",
        lambda *_a, **kw: captured.update(kw),
    )
    row = {
        "id": registration_id, "thread_id": "thread-x",
        "registered_at": "2026-01-01T00:00:00Z",
    }
    restart_wakes._materialize_row(_FakeConnection(), row, kind=kind)
    return captured["message_id"]


def _dead_letter_id(monkeypatch, delivery_id: str) -> str:
    connection = _FakeConnection()
    monkeypatch.setattr(
        restart_webhooks, "write_transaction", _fake_write_transaction(connection)
    )
    row = {
        "delivery_id": delivery_id, "event_type": "push",
        "payload_json": "{}", "created_at": "2026-01-01T00:00:00Z",
    }
    assert restart_webhooks._dead_letter(row, "claim", reason="r") is True
    insert = [c for c in connection.calls if c[0].startswith("INSERT")]
    return insert[0][1][0]


async def _send_ref_id(monkeypatch, source_input_id: str) -> str:
    connection = MagicMock()
    connection.execute.return_value.fetchone.side_effect = [(1,), None]
    monkeypatch.setattr(
        delegation_markers, "write_transaction", _fake_write_transaction(connection)
    )
    monkeypatch.setattr(delegation_markers, "insert_message", lambda *a, **k: None)

    async def _run(fn):
        return fn()

    async def _broadcast(*_a, **_k):
        return None

    monkeypatch.setattr(delegation_markers, "run_in_db", _run)
    monkeypatch.setattr(delegation_markers, "broadcast_message_rows", _broadcast)
    return await delegation_markers.emit_send_ref_marker(
        "sender-1", "target-1", "Target", None, source_input_id=source_input_id,
    )


def _registration_event_id(artifact_id: str, event_type: str, causal: str) -> str:
    return registration_events._event_values(
        artifact_id=artifact_id, event_type=event_type, actor_thread_id=None,
        authority=None, reason=None, source_path=None, affected_thread_id=None,
        old_owner_thread_id=None, new_owner_thread_id=None,
        old_relationship_state=None, new_relationship_state=None,
        metadata=None, now="2026-01-01T00:00:00Z", causal_event_id=causal,
    )[0]


async def _observe(monkeypatch) -> dict[str, str]:
    return {
        "restart_registration_a": restart_wakes._registration_id(None, "thread-1", "turn-1"),
        "restart_registration_b": restart_wakes._registration_id("op-9", "thread-2", "turn-2"),
        "restart_input_a": _restart_input_id(monkeypatch, "restart_resume", "reg-1"),
        "restart_input_b": _restart_input_id(monkeypatch, "restart_recovery", "reg-2"),
        "dead_letter_a": _dead_letter_id(monkeypatch, "delivery-1"),
        "dead_letter_b": _dead_letter_id(monkeypatch, "delivery-2"),
        "send_ref_a": await _send_ref_id(monkeypatch, "input-1"),
        "send_ref_b": await _send_ref_id(monkeypatch, "input-2"),
        "registration_event_a": _registration_event_id("art-1", "registered", "cause-1"),
        "registration_event_b": _registration_event_id("art-2", "rehomed", "cause-2"),
        "cc_transcript_a": cc_transcript_projection._stable_uuid("sess-1", "msg-1", "user"),
        "cc_transcript_b": cc_transcript_projection._stable_uuid("sess-2", "msg-2", "assistant"),
    }


async def test_persisted_uuid5_seeds_are_byte_stable(monkeypatch) -> None:
    observed = await _observe(monkeypatch)
    assert observed == EXPECTED


@pytest.mark.parametrize(
    ("module", "name", "value"),
    [
        (restart_wakes, "_REGISTRATION_SEED", "corsia-restart"),
        (restart_wakes, "_INPUT_SEED", "corsia-restart-input"),
        (restart_webhooks, "_DEAD_LETTER_SEED", "corsia-webhook-dead-letter"),
        (delegation_markers, "_SEND_REF_SEED", "corsia-send-ref:"),
        (registration_events, "_REGISTRATION_EVENT_SEED", "corsia-artifact-registration"),
        (
            cc_transcript_projection,
            "_TRANSCRIPT_SEED",
            "corsia:claude-code:reconstructed-transcript:",
        ),
    ],
)
def test_seed_constants_keep_exact_bytes(module, name, value) -> None:
    assert getattr(module, name) == value


@pytest.mark.parametrize("env", [None, "custom-project"])
def test_analytics_project_default_label(monkeypatch, env) -> None:
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    if env is None:
        monkeypatch.delenv("CORSIA_ANALYTICS_PROJECT", raising=False)
    else:
        monkeypatch.setenv("CORSIA_ANALYTICS_PROJECT", env)
    connection = _FakeConnection()
    monkeypatch.setattr(product_events.analytics_db, "get_db", lambda: connection)
    product_events.emit_product_event("cat", "act")
    params = connection.calls[0][1]
    assert params[-1] == (env or "corsia")


def test_managed_process_message_ids_keep_exact_bytes() -> None:
    assert managed_process_events._message_id("handle-1", "exit") == _uuid5(
        "corsia-managed-process:handle-1:exit"
    )
    assert managed_process_events._message_id("handle-2", "stall") == _uuid5(
        "corsia-managed-process:handle-2:stall"
    )


def test_derived_cc_transcript_message_id_keeps_exact_bytes() -> None:
    row_uuid = "0f8b2c4e-1a2b-4c3d-9e8f-7a6b5c4d3e2f"
    assert cc_transcript_projection._assistant_message_id(row_uuid) == (
        "msg_corsia_0f8b2c4e1a2b4c3d9e8f7a6b"
    )
