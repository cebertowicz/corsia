from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

import pytest

from infra.schema import ARTIFACT_IDENTITY_SCHEMA_SQL


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            history_epoch INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            seq INTEGER,
            active_turn_id TEXT,
            UNIQUE(thread_id, seq)
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
        + ARTIFACT_IDENTITY_SCHEMA_SQL
    )
    db.execute("INSERT INTO threads (id, history_epoch) VALUES ('thread-1', 7)")
    return db


def _turn(db: sqlite3.Connection, turn_id: str, status: str) -> None:
    db.execute(
        "INSERT INTO active_turns (id, thread_id, status, started_at, ended_at) "
        "VALUES (?, 'thread-1', ?, 'start', ?)",
        (turn_id, status, None if status == "running" else "end"),
    )


def _message(
    db: sqlite3.Connection,
    message_id: str,
    seq: int | None,
    role: str,
    content: str,
    *,
    turn_id: str | None,
    metadata: dict | None = None,
    created_at: str | None = None,
    delivery_state: str = "delivered",
) -> None:
    meta = dict(metadata or {})
    if turn_id is not None:
        meta["turn_id"] = turn_id
    db.execute(
        "INSERT INTO messages "
        "(id, thread_id, role, content, metadata, created_at,delivery_state,seq,"
        "active_turn_id) VALUES (?, 'thread-1', ?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            role,
            content,
            json.dumps(meta) if meta else None,
            created_at or f"time-{100 - seq if seq is not None else 'pending'}",
            delivery_state,
            seq,
            turn_id,
        ),
    )


def _seed_golden(db: sqlite3.Connection) -> None:
    _turn(db, "turn-1", "completed")
    _turn(db, "turn-2", "interrupted")
    _turn(db, "turn-active", "running")
    _message(db, "u1", 1, "user", "inspect", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "checking",
        turn_id="turn-1",
        metadata={
            "step_type": "tool_calls",
            "llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"path":"a"}'},
            }],
            "reasoning": "portable reasoning",
            "reasoning_provenance": {
                "source_harness": "native",
                "source_provider": "omlx",
                "representation_kind": "reasoning_content",
            },
            "attachments": [{"artifact_id": "artifact-1", "path": "docs/a.md"}],
        },
    )
    _message(
        db,
        "r1",
        3,
        "tool",
        "file contents",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-1",
            "assistant_step_id": "a1",
            "name": "Read",
            "artifact_references": [{
                "artifact_id": "artifact-result-1",
                "path": "docs/result.md",
            }],
        },
    )
    _message(db, "a2", 4, "assistant", "done", turn_id="turn-1")
    _message(
        db,
        "audit",
        5,
        "system",
        "turn complete",
        turn_id="turn-1",
        metadata={"type": "result"},
    )
    _message(db, "u2", 6, "user", "stop", turn_id="turn-2")
    _message(
        db,
        "a3",
        7,
        "assistant",
        "",
        turn_id="turn-2",
        metadata={
            "step_type": "tool_calls",
            "llm_tool_calls": [{
                "id": "call-2",
                "type": "function",
                "function": {"name": "Bash", "arguments": "{}"},
            }],
        },
    )
    _message(
        db,
        "r2",
        8,
        "tool",
        "cancelled",
        turn_id="turn-2",
        metadata={
            "tool_call_id": "call-2",
            "assistant_step_id": "a3",
            "name": "Bash",
            "synthetic_tool_result": True,
            "synthetic_reason": "user_interruption",
        },
    )
    _message(
        db,
        "interrupt-marker",
        9,
        "system",
        "",
        turn_id="turn-2",
        metadata={"type": "result", "interrupted": True},
    )
    _message(db, "u3", 10, "user", "new work", turn_id="turn-active")
    db.commit()


def test_canonical_projection_preserves_complete_turns_and_typed_semantics() -> None:
    from history.projection import render_portable_history
    from history.adapters import to_native_history

    db = _db()
    _seed_golden(db)

    projection = render_portable_history("thread-1", db=db)

    assert projection.format_version == 1
    assert projection.history_epoch == 7
    assert projection.start_exclusive is None
    # Non-semantic lifecycle rows cannot move a durable checkpoint boundary.
    assert projection.sealed_boundary.seq == 8
    assert projection.sealed_boundary.message_id == "r2"
    assert [turn.status for turn in projection.turns] == ["completed", "interrupted"]
    assert [message.message_id for message in projection.messages] == [
        "u1", "a1", "r1", "a2", "u2", "a3", "r2",
    ]
    assert projection.full_raw_replay_available is True

    assistant = projection.messages[1]
    assert assistant.reasoning_content == "portable reasoning"
    assert assistant.reasoning_provenance["representation_kind"] == "reasoning_content"
    assert assistant.artifact_references == (
        {"artifact_id": "artifact-1", "path": "docs/a.md"},
    )
    assert projection.messages[-1].synthetic_reason == "user_interruption"

    native = to_native_history(projection)
    assert [message["role"] for message in native] == [
        "user", "assistant", "tool", "assistant", "user", "assistant", "tool",
    ]
    assert native[1]["reasoning_content"] == "portable reasoning"
    assert native[1]["tool_calls"][0]["id"] == "call-1"
    assert native[2]["tool_call_id"] == "call-1"


def test_cross_harness_replay_projects_cc_tool_names_to_destination_surface() -> None:
    """A CC tail must not teach Native or Codex the CC-only MCP name."""
    from history.adapters import to_native_history
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import cc_snapshot_history
    from subagents.codex_context_reconstruction import codex_inject_items

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "inspect", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "checking",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "mcp__corsia__read_file",
                    "arguments": '{"path":"README.md"}',
                },
            }],
        },
    )
    _message(
        db,
        "r1",
        3,
        "tool",
        "contents",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-1",
            "assistant_step_id": "a1",
            "name": "mcp__corsia__read_file",
        },
    )
    db.commit()
    projection = render_portable_history("thread-1", db=db)

    native = to_native_history(projection)
    cc = cc_snapshot_history(
        projection,
        cc_session_id="cc-destination",
        allowed_corsia_tool_names=frozenset({"read_file"}),
    )
    codex = codex_inject_items(projection)

    assert native[1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert native[2]["name"] == "read_file"
    assert cc[1]["content"][1]["name"] == "mcp__corsia__read_file"
    assert '"name": "read_file"' in codex[1]["content"][0]["text"]
    assert codex[2]["content"][0]["text"].startswith(
        "[tool result: read_file; call_id=call-1]"
    )
    assert projection.messages[1].tool_calls[0]["function"]["name"] == (
        "mcp__corsia__read_file"
    )


@pytest.mark.parametrize("disposition", ["wake", "passive"])
def test_context_event_projection_is_once_and_equal_across_adapters(disposition) -> None:
    from history.adapters import to_native_history
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import cc_snapshot_history
    from subagents.codex_context_reconstruction import codex_inject_items

    db = _db()
    _turn(db, "turn-event", "completed")
    _message(
        db, "event", 1, "system", "CANONICAL_CONTEXT_EVENT",
        turn_id="turn-event",
        metadata={"type": "context_event", "event_disposition": disposition},
    )
    _message(db, "answer", 2, "assistant", "ack", turn_id="turn-event")
    db.commit()
    projection = render_portable_history("thread-1", db=db)

    rendered = (
        json.dumps(to_native_history(projection)),
        json.dumps(cc_snapshot_history(projection, cc_session_id="cc-event")),
        json.dumps(codex_inject_items(projection)),
    )
    assert all(value.count("CANONICAL_CONTEXT_EVENT") == 1 for value in rendered)


def test_rerun_turn_replays_its_note_as_inbound_input_on_every_adapter() -> None:
    """A re-run Turn begins with its model-only note, never with a person row."""
    from core.message_history import db_to_llm_history
    from history.adapters import to_native_history
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import cc_snapshot_history
    from subagents.codex_context_reconstruction import codex_inject_items

    db = _db()
    _turn(db, "turn-1", "error")
    _message(db, "u1", 1, "user", "question", turn_id="turn-1")
    _turn(db, "turn-2", "completed")
    _message(
        db, "turn-rerun:turn-1", 2, "system", "CANONICAL_RERUN_NOTE",
        turn_id="turn-2", metadata={"type": "turn_rerun", "failed_turn_id": "turn-1"},
    )
    _message(db, "a2", 3, "assistant", "answer", turn_id="turn-2")
    db.commit()
    projection = render_portable_history("thread-1", db=db)

    assert [turn.messages[0].role for turn in projection.turns] == ["user", "user"]
    rendered = (
        json.dumps(to_native_history(projection)),
        json.dumps(cc_snapshot_history(projection, cc_session_id="cc-rerun")),
        json.dumps(codex_inject_items(projection)),
    )
    assert all(value.count("CANONICAL_RERUN_NOTE") == 1 for value in rendered)
    rows = [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY seq")]
    assert {"role": "user", "content": "CANONICAL_RERUN_NOTE"} in db_to_llm_history(rows)


def test_completed_turn_boundary_survives_later_nonsemantic_system_rows() -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "question", turn_id="turn-1")
    _message(db, "a1", 2, "assistant", "answer", turn_id="turn-1")
    _message(
        db,
        "result-1",
        3,
        "system",
        "turn complete",
        turn_id="turn-1",
        metadata={"type": "result"},
    )
    db.commit()

    persisted_boundary = render_portable_history(
        "thread-1",
        db=db,
    ).sealed_boundary
    assert persisted_boundary is not None

    _message(
        db,
        "audit-later",
        4,
        "system",
        "late audit",
        turn_id="turn-1",
        metadata={"type": "audit"},
    )
    db.commit()

    tail = render_portable_history(
        "thread-1",
        db=db,
        start_exclusive=persisted_boundary,
    )

    assert persisted_boundary.message_id == "a1"
    assert tail.sealed_boundary == persisted_boundary
    assert tail.turns == ()


def test_turn_owner_lookup_reads_only_the_named_turns() -> None:
    from history.projection import render_portable_history

    db = _db()
    _seed_golden(db)
    statements: list[str] = []
    db.set_trace_callback(statements.append)

    render_portable_history("thread-1", db=db)

    owner_queries = [
        statement
        for statement in statements
        if "FROM active_turns" in statement
    ]
    assert len(owner_queries) == 1
    assert "WHERE thread_id = 'thread-1'" in owner_queries[0]
    assert "id IN ('turn-1','turn-2','turn-active')" in owner_queries[0]


def test_adapters_preserve_assistant_and_tool_artifact_references_without_reordering(
) -> None:
    from history.projection import render_portable_history
    from history.adapters import to_native_history
    from subagents.cc_context_reconstruction import cc_snapshot_history
    from subagents.codex_context_reconstruction import codex_inject_items

    db = _db()
    _seed_golden(db)
    projection = render_portable_history("thread-1", db=db)
    source_identity = (
        projection.start_exclusive,
        projection.sealed_boundary,
        tuple((message.message_id, message.seq) for message in projection.messages),
    )

    native = to_native_history(projection)
    cc = cc_snapshot_history(projection, cc_session_id="cc-artifact-parity")
    codex = codex_inject_items(projection)

    assert [message["role"] for message in native] == [
        "user", "assistant", "tool", "assistant", "user", "assistant", "tool",
    ]
    assert [message["role"] for message in cc] == [
        "user", "assistant", "user", "assistant", "user", "assistant", "user",
    ]
    assert [message["role"] for message in codex] == [
        "user", "assistant", "user", "assistant", "user", "assistant", "user",
    ]

    native_assistant = native[1]["content"]
    native_tool = native[2]["content"]
    cc_assistant = cc[1]["content"][0]["text"]
    cc_tool = cc[2]["content"][0]["content"][0]["text"]
    codex_assistant = codex[1]["content"][0]["text"]
    codex_tool = codex[2]["content"][0]["text"]
    for rendered in (native_assistant, cc_assistant):
        assert "[CORSIA artifact references]" in rendered
        assert "artifact-1" in rendered
        assert "docs/a.md" in rendered
    for rendered in (native_tool, cc_tool):
        assert "[CORSIA artifact references]" in rendered
        assert "artifact-result-1" in rendered
        assert "docs/result.md" in rendered
    assert "artifact-1" in codex_assistant
    assert "docs/a.md" in codex_assistant
    assert "artifact-result-1" in codex_tool
    assert "docs/result.md" in codex_tool

    assert (
        projection.start_exclusive,
        projection.sealed_boundary,
        tuple((message.message_id, message.seq) for message in projection.messages),
    ) == source_identity


def test_native_user_artifact_references_survive_provider_sanitization() -> None:
    from history.projection import render_portable_history
    from history.adapters import to_native_history
    from llm.base import sanitize_messages

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(
        db,
        "u1",
        1,
        "user",
        "inspect this",
        turn_id="turn-1",
        metadata={
            "attachments": [{
                "artifact_id": "artifact-user-1",
                "path": "docs/user-input.md",
            }],
        },
    )
    _message(db, "a1", 2, "assistant", "done", turn_id="turn-1")
    db.commit()

    native = to_native_history(render_portable_history("thread-1", db=db))
    provider_messages = sanitize_messages(native)

    assert "[CORSIA artifact references]" in provider_messages[0]["content"]
    assert "artifact-user-1" in provider_messages[0]["content"]
    assert "docs/user-input.md" in provider_messages[0]["content"]


def test_tail_is_strictly_after_a_complete_same_epoch_turn_boundary() -> None:
    from history.projection import render_portable_history

    db = _db()
    _seed_golden(db)
    full = render_portable_history("thread-1", db=db)

    tail = render_portable_history(
        "thread-1", db=db, start_exclusive=full.turns[0].boundary,
    )

    assert tail.start_exclusive == full.turns[0].boundary
    assert [turn.turn_id for turn in tail.turns] == ["turn-2"]
    assert [message.seq for message in tail.messages] == [6, 7, 8]
    assert tail.sealed_boundary == full.sealed_boundary


@pytest.mark.parametrize(
    "replace,match",
    [
        ({"thread_id": "other"}, "thread"),
        ({"history_epoch": 6}, "epoch"),
        ({"message_id": "wrong"}, "message"),
        ({"seq": 2, "message_id": "a1"}, "Turn boundary"),
    ],
)
def test_invalid_or_mid_turn_boundaries_fail_closed(replace: dict, match: str) -> None:
    from history.types import (
        HistoryBoundary,
        PortableHistoryError,
    )
    from history.projection import render_portable_history

    db = _db()
    _seed_golden(db)
    boundary = {
        "thread_id": "thread-1",
        "history_epoch": 7,
        "seq": 4,
        "message_id": "a2",
    }
    boundary.update(replace)

    with pytest.raises(PortableHistoryError, match=match):
        render_portable_history(
            "thread-1", db=db, start_exclusive=HistoryBoundary(**boundary),
        )


def _seed_terminal_turn_with_lost_result(db: sqlite3.Connection) -> None:
    """One completed Turn whose second declared call never got a result row.

    This is the exact durable shape a mid-batch provider death leaves behind
    (session af204877, 2026-08-13): the Assistant Step declares the batch, some
    results landed, the process died before the rest, and no repair row exists.
    """
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "go", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "calling",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [
                {
                    "id": "call-done",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                },
                {
                    "id": "call-lost",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": "{}"},
                },
            ],
        },
    )
    _message(
        db,
        "t1",
        3,
        "tool",
        "read output",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-done",
            "assistant_step_id": "a1",
            "name": "Read",
        },
    )
    db.commit()


def test_terminal_turn_missing_tool_result_projects_missing_stub() -> None:
    """A terminal Turn missing one declared call's result still renders.

    Desired-behavior contract §3.10 / Central Journey: one ill-formed step
    must never make the whole thread unrenderable and unserviceable. The
    declared call projects the canonical missing-result stub — truthfully
    marked synthetic — so delivery and folds keep working while the defect
    stays visible.
    """
    from history.projection import render_portable_history

    db = _db()
    _seed_terminal_turn_with_lost_result(db)

    projection = render_portable_history("thread-1", db=db)

    [turn] = projection.turns
    assert [message.role for message in turn.messages] == [
        "user", "assistant", "tool", "tool",
    ]
    real, stub = turn.messages[2], turn.messages[3]
    assert real.tool_call_id == "call-done"
    assert real.synthetic_tool_result is False
    assert stub.tool_call_id == "call-lost"
    assert stub.assistant_step_id == "a1"
    assert stub.tool_name == "Bash"
    assert stub.synthetic_tool_result is True
    assert stub.synthetic_reason == "tool_result_missing"
    assert "No result was recorded" in stub.content


def test_every_lost_call_is_counted_not_collapsed_by_step() -> None:
    """A Step that lost two results declares two, not one.

    Stubs anchor at their Assistant Step's seq, so deduplicating by seq would
    tell the model one result was lost when twelve were — the exact shape of
    the af204877 batches.
    """
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "go", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "calling",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [
                {
                    "id": f"call-lost-{index}",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": "{}"},
                }
                for index in range(2)
            ],
        },
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert projection.missing_result_stub_seqs == (2, 2)


def test_missing_tool_result_stub_keeps_fold_units_pairable() -> None:
    """The stub pairs into fold units, so a fold over this Turn settles."""
    from history.folds import checkpoint_fold_units
    from history.projection import render_portable_history

    db = _db()
    _seed_terminal_turn_with_lost_result(db)

    projection = render_portable_history("thread-1", db=db)
    units = checkpoint_fold_units(projection)

    assert len(units) == 1
    call_ids = [
        message.tool_call_id
        for message in units[0].messages
        if message.role == "tool"
    ]
    assert call_ids == ["call-done", "call-lost"]


def test_active_turn_before_later_sealed_data_fails_closed() -> None:
    from history.types import PortableHistoryError
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-active", "running")
    _turn(db, "turn-later", "completed")
    _message(db, "u1", 1, "user", "active", turn_id="turn-active")
    _message(db, "u2", 2, "user", "later", turn_id="turn-later")
    _message(db, "a2", 3, "assistant", "answer", turn_id="turn-later")
    db.commit()

    with pytest.raises(PortableHistoryError, match="unsealed Turn"):
        render_portable_history("thread-1", db=db)


def test_retention_stub_is_reported_and_still_renders(tmp_path) -> None:
    """A holed transcript is EVIDENCE, not a veto — every harness renders it.

    Refusing to translate a retention-holed projection is what left a trimmed
    thread with no replay at all once its covering checkpoint went bad, and the
    send died on a 409 nothing could clear. The stub carries its own marker and
    the selection that chose it declares the loss in band, so the honest answer
    is to render it and say what is gone.
    """
    from history.projection import render_portable_history
    from history.adapters import to_native_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "go", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        },
    )
    _message(
        db,
        "r1",
        3,
        "tool",
        "[tool output trimmed after 30d retention — 200 chars, 4 lines]",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-1",
            "assistant_step_id": "a1",
            "name": "Read",
        },
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert projection.full_raw_replay_available is False
    assert projection.retention_stub_seqs == (3,)

    from subagents.cc_context_reconstruction import (
        reconstruct_cc_transcript_from_corsia,
    )
    from subagents.codex_context_reconstruction import codex_inject_items

    native = to_native_history(projection)
    assert any(
        "tool output trimmed" in json.dumps(message) for message in native
    )
    codex = codex_inject_items(projection)
    assert any(
        "tool output trimmed" in json.dumps(item) for item in codex
    )
    transcript_path = tmp_path / "claude" / "projects" / "-workspace" / "cc.jsonl"
    transcript = reconstruct_cc_transcript_from_corsia(
        projection=projection,
        cc_session_id="cc-retained",
        path=str(transcript_path),
        working_dir="/workspace",
    )
    assert transcript.full_raw_replay_available is False
    assert "tool output trimmed" in transcript_path.read_text()


def test_tool_results_preserve_durable_sequence_not_call_declaration_order() -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "go", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "One", "arguments": "{}"},
                },
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "Two", "arguments": "{}"},
                },
            ],
        },
    )
    for message_id, seq, call_id in (
        ("result-2", 3, "call-2"),
        ("result-1", 4, "call-1"),
    ):
        _message(
            db,
            message_id,
            seq,
            "tool",
            message_id,
            turn_id="turn-1",
            metadata={
                "tool_call_id": call_id,
                "assistant_step_id": "a1",
                "name": "Tool",
            },
        )
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert [message.seq for message in projection.messages] == [1, 2, 3, 4]
    assert [message.message_id for message in projection.messages] == [
        "u1", "a1", "result-2", "result-1",
    ]


def test_late_tool_result_stays_atomic_with_its_assistant_step() -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "interrupted")
    _message(db, "u1", 1, "user", "go", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        },
    )
    _message(db, "interrupt", 3, "user", "stop", turn_id="turn-1")
    _message(db, "a2", 4, "assistant", "stopped", turn_id="turn-1")
    _message(
        db,
        "r1",
        5,
        "tool",
        "completed before interruption",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-1",
            "assistant_step_id": "a1",
            "name": "Read",
        },
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert [message.message_id for message in projection.messages] == [
        "u1", "a1", "r1", "interrupt", "a2",
    ]


def test_agent_update_between_a_call_and_its_result_projects_and_keeps_the_pair() -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "go", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "post_update", "arguments": "{}"},
            }],
        },
    )
    # The row post_update writes while its own call is still running.
    _message(
        db, "update", 3, "assistant", "Focused check passed.",
        turn_id="turn-1", metadata={"for_user": True},
    )
    _message(
        db,
        "r1",
        4,
        "tool",
        "Update saved as message update.",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-1",
            "assistant_step_id": "a1",
            "name": "post_update",
        },
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    ids = [message.message_id for message in projection.messages]
    assert ids.index("r1") == ids.index("a1") + 1
    assert "update" in ids
    assert "Focused check passed." in json.dumps(
        [asdict(message) for message in projection.messages], default=str
    )


def test_checkpoint_fold_units_keep_steps_with_complete_tool_batches() -> None:
    from history.folds import checkpoint_fold_units
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _turn(db, "turn-2", "completed")
    _message(db, "u1", 1, "user", "inspect", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "reading",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"path":"a"}'},
            }],
        },
    )
    _message(
        db,
        "r1",
        3,
        "tool",
        "first result",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-1",
            "assistant_step_id": "a1",
            "name": "Read",
        },
    )
    _message(db, "u2", 4, "user", "also inspect b", turn_id="turn-1")
    _message(
        db,
        "a2",
        5,
        "assistant",
        "reading again",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-2",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"path":"b"}'},
            }],
        },
    )
    _message(
        db,
        "r2",
        6,
        "tool",
        "second result",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-2",
            "assistant_step_id": "a2",
            "name": "Read",
        },
    )
    _message(db, "a3", 7, "assistant", "done", turn_id="turn-1")
    _message(db, "u3", 8, "user", "next Turn", turn_id="turn-2")
    _message(db, "a4", 9, "assistant", "next answer", turn_id="turn-2")
    db.commit()

    projection = render_portable_history("thread-1", db=db)
    units = checkpoint_fold_units(projection)

    assert [unit.ordinal for unit in units] == [0, 1, 2, 3]
    assert [
        [message.message_id for message in unit.messages]
        for unit in units
    ] == [
        ["u1", "a1", "r1"],
        ["u2", "a2", "r2"],
        ["a3"],
        ["u3", "a4"],
    ]
    assert [unit.turn_id for unit in units] == ["turn-1"] * 3 + ["turn-2"]
    assert [unit.status for unit in units] == ["completed"] * 4
    assert [unit.legacy_reason for unit in units] == [None] * 4
    assert [unit.completes_turn for unit in units] == [False, False, True, True]
    assert [unit.boundary for unit in units] == [
        None,
        None,
        projection.turns[0].boundary,
        projection.turns[1].boundary,
    ]


@pytest.mark.parametrize("status", ["interrupted", "error"])
def test_checkpoint_fold_units_preserve_user_only_terminal_turn(status: str) -> None:
    from history.folds import checkpoint_fold_units
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", status)
    _message(db, "u1", 1, "user", "accepted input", turn_id="turn-1")
    db.commit()

    projection = render_portable_history("thread-1", db=db)
    units = checkpoint_fold_units(projection)

    assert len(units) == 1
    assert [message.message_id for message in units[0].messages] == ["u1"]
    assert units[0].turn_id == "turn-1"
    assert units[0].status == status
    assert units[0].legacy_reason is None
    assert units[0].completes_turn is True
    assert units[0].boundary == projection.turns[0].boundary


def test_checkpoint_fold_fragments_split_each_payload_under_original_identity() -> None:
    from history.folds import (
        checkpoint_fold_units,
        fragment_checkpoint_fold_unit,
    )
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "USER-1234", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "ASSISTANT",
        turn_id="turn-1",
        metadata={
            "attachments": [
                {
                    "artifact_id": "artifact-a1",
                    "path": "documents/assistant-result.md",
                }
            ],
            "sender_kind": "agent",
            "from_thread": "source-thread",
            "llm_tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "ARGUMENT-ONE"},
                },
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "ARGUMENT-TWO"},
                },
            ],
        },
    )
    # Result order is durable transcript order, independent of call declaration order.
    _message(
        db,
        "r2",
        3,
        "tool",
        "RESULT-TWO",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-2",
            "assistant_step_id": "a1",
            "name": "Read",
        },
    )
    _message(
        db,
        "r1",
        4,
        "tool",
        "RESULT-ONE",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-1",
            "assistant_step_id": "a1",
            "name": "Read",
        },
    )
    db.commit()

    unit = checkpoint_fold_units(render_portable_history("thread-1", db=db))[0]
    fragments = fragment_checkpoint_fold_unit(unit, max_payload_bytes=4)

    assert fragments == fragment_checkpoint_fold_unit(
        unit,
        max_payload_bytes=4,
    )
    first_parts = [fragment for fragment in fragments if fragment.part_index == 1]
    assert {fragment.turn_status for fragment in fragments} == {"completed"}
    assert {fragment.turn_legacy_reason for fragment in fragments} == {None}
    assert [
        (
            fragment.payload_kind,
            fragment.message_id,
            fragment.assistant_step_id,
            fragment.tool_call_id,
            fragment.role,
            fragment.tool_name,
        )
        for fragment in first_parts
    ] == [
        ("user_text", "u1", None, None, "user", None),
        ("assistant_text", "a1", "a1", None, "assistant", None),
        ("tool_arguments", "a1", "a1", "call-1", "assistant", "Read"),
        ("tool_arguments", "a1", "a1", "call-2", "assistant", "Read"),
        ("tool_result", "r2", "a1", "call-2", "tool", "Read"),
        ("tool_result", "r1", "a1", "call-1", "tool", "Read"),
    ]
    expected = {
        ("user_text", "u1", None): "USER-1234",
        ("assistant_text", "a1", None): "ASSISTANT",
        ("tool_arguments", "a1", "call-1"): "ARGUMENT-ONE",
        ("tool_arguments", "a1", "call-2"): "ARGUMENT-TWO",
        ("tool_result", "r2", "call-2"): "RESULT-TWO",
        ("tool_result", "r1", "call-1"): "RESULT-ONE",
    }
    for key, source_text in expected.items():
        parts = [
            fragment
            for fragment in fragments
            if (
                fragment.payload_kind,
                fragment.message_id,
                fragment.tool_call_id,
            ) == key
        ]
        assert "".join(part.text for part in parts) == source_text
        assert [part.part_index for part in parts] == list(
            range(1, parts[0].part_count + 1)
        )
        assert all(part.part_count == len(parts) for part in parts)
        assert [part.part_label for part in parts] == [
            f"part {index}/{len(parts)}"
            for index in range(1, len(parts) + 1)
        ]
        assert all(part.byte_count <= 4 for part in parts)

    assistant_fragments = [
        fragment for fragment in fragments if fragment.message_id == "a1"
    ]
    metadata_fragments = [
        fragment
        for fragment in assistant_fragments
        if fragment.artifact_references or fragment.inbound_source
    ]
    assert metadata_fragments == [assistant_fragments[0]]
    assert assistant_fragments[0].payload_kind == "assistant_text"
    assert assistant_fragments[0].part_index == 1
    assert assistant_fragments[0].artifact_references == (
        {
            "artifact_id": "artifact-a1",
            "path": "documents/assistant-result.md",
        },
    )
    assert assistant_fragments[0].inbound_source == {
        "sender_kind": "agent",
        "from_thread": "source-thread",
    }
    assert all(
        fragment.artifact_references == () and fragment.inbound_source == {}
        for fragment in assistant_fragments[1:]
    )


def test_checkpoint_fold_fragments_split_on_utf8_character_boundaries() -> None:
    from history.folds import (
        checkpoint_fold_units,
        fragment_checkpoint_fold_unit,
    )
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "interrupted")
    _message(db, "u1", 1, "user", "A🙂BéC", turn_id="turn-1")
    db.commit()

    unit = checkpoint_fold_units(render_portable_history("thread-1", db=db))[0]
    fragments = fragment_checkpoint_fold_unit(unit, max_payload_bytes=5)

    assert [fragment.text for fragment in fragments] == ["A🙂", "BéC"]
    assert [fragment.byte_count for fragment in fragments] == [5, 4]
    assert [fragment.part_label for fragment in fragments] == [
        "part 1/2",
        "part 2/2",
    ]


def test_checkpoint_fold_fragments_preserve_artifact_references_and_identity() -> None:
    from history.folds import (
        checkpoint_fold_units,
        fragment_checkpoint_fold_unit,
    )
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    artifacts = [
        {
            "artifact_id": "artifact-1",
            "path": "documents/result.md",
            "filename": "result.md",
        }
    ]
    _message(
        db,
        "u1",
        1,
        "user",
        "USER-1234",
        turn_id="turn-1",
        metadata={"attachments": artifacts},
        created_at="user-created-at",
    )
    _message(db, "a1", 2, "assistant", "done", turn_id="turn-1")
    db.commit()

    unit = checkpoint_fold_units(render_portable_history("thread-1", db=db))[0]
    fragments = fragment_checkpoint_fold_unit(unit, max_payload_bytes=4)
    user_fragments = [
        fragment for fragment in fragments if fragment.message_id == "u1"
    ]

    assert [fragment.text for fragment in user_fragments] == ["USER", "-123", "4"]
    assert [fragment.part_index for fragment in user_fragments] == [1, 2, 3]
    assert all(fragment.part_count == 3 for fragment in user_fragments)
    assert all(fragment.unit_ordinal == unit.ordinal for fragment in user_fragments)
    assert all(fragment.message_seq == 1 for fragment in user_fragments)
    assert all(
        fragment.message_created_at == "user-created-at"
        for fragment in user_fragments
    )
    assert user_fragments[0].artifact_references == tuple(artifacts)
    assert all(
        fragment.artifact_references == () for fragment in user_fragments[1:]
    )
    assert all(fragment.inbound_source == {} for fragment in user_fragments)


def test_checkpoint_fold_fragments_preserve_inbound_source_and_identity() -> None:
    from history.folds import (
        checkpoint_fold_units,
        fragment_checkpoint_fold_unit,
    )
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    inbound_source = {
        "sender_kind": "agent",
        "from_thread": "source-thread",
        "from_title": "Research",
        "scope": "project-a",
        "routed_event": "delegation.completed",
    }
    _message(
        db,
        "u1",
        1,
        "user",
        "USER-1234",
        turn_id="turn-1",
        metadata=inbound_source,
        created_at="user-created-at",
    )
    _message(db, "a1", 2, "assistant", "done", turn_id="turn-1")
    db.commit()

    unit = checkpoint_fold_units(render_portable_history("thread-1", db=db))[0]
    fragments = fragment_checkpoint_fold_unit(unit, max_payload_bytes=4)
    user_fragments = [
        fragment for fragment in fragments if fragment.message_id == "u1"
    ]

    assert [fragment.text for fragment in user_fragments] == ["USER", "-123", "4"]
    assert [fragment.part_index for fragment in user_fragments] == [1, 2, 3]
    assert all(fragment.part_count == 3 for fragment in user_fragments)
    assert all(fragment.unit_ordinal == unit.ordinal for fragment in user_fragments)
    assert all(fragment.message_seq == 1 for fragment in user_fragments)
    assert all(
        fragment.message_created_at == "user-created-at"
        for fragment in user_fragments
    )
    assert all(fragment.artifact_references == () for fragment in user_fragments)
    assert user_fragments[0].inbound_source == inbound_source
    assert all(fragment.inbound_source == {} for fragment in user_fragments[1:])


def test_checkpoint_fold_fragment_metadata_has_linear_serialized_cost() -> None:
    from history.folds import (
        checkpoint_fold_units,
        fragment_checkpoint_fold_unit,
    )
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "interrupted")
    artifacts = [
        {
            "artifact_id": "artifact-1",
            "url": "a" * 100_000,
        }
    ]
    inbound_source = {
        "sender_kind": "agent",
        "from_title": "b" * 100_000,
    }
    _message(
        db,
        "u1",
        1,
        "user",
        "USER-1234" * 8,
        turn_id="turn-1",
        metadata={
            "attachments": artifacts,
            **inbound_source,
        },
    )
    db.commit()

    unit = checkpoint_fold_units(render_portable_history("thread-1", db=db))[0]
    fragments = fragment_checkpoint_fold_unit(unit, max_payload_bytes=4)

    metadata_fragments = [
        fragment
        for fragment in fragments
        if fragment.artifact_references or fragment.inbound_source
    ]
    assert metadata_fragments == [fragments[0]]
    assert fragments[0].part_index == 1
    assert fragments[0].artifact_references == tuple(artifacts)
    assert fragments[0].inbound_source == inbound_source

    def serialized_bytes(value: object) -> int:
        return len(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )

    serialized_fragments = [asdict(fragment) for fragment in fragments]
    metadata_free_fragments = [
        {
            **fragment,
            "artifact_references": (),
            "inbound_source": {},
        }
        for fragment in serialized_fragments
    ]
    actual_total = sum(serialized_bytes(fragment) for fragment in serialized_fragments)
    metadata_free_total = sum(
        serialized_bytes(fragment) for fragment in metadata_free_fragments
    )
    one_metadata_delta = serialized_bytes(
        {
            "artifact_references": tuple(artifacts),
            "inbound_source": inbound_source,
        }
    ) - serialized_bytes(
        {
            "artifact_references": (),
            "inbound_source": {},
        }
    )

    assert len(fragments) == 18
    assert actual_total == metadata_free_total + one_metadata_delta


def test_duplicate_result_delivery_emits_one_real_winner_but_advances_boundary() -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "interrupted")
    _message(db, "u1", 1, "user", "go", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        },
    )
    common = {
        "tool_call_id": "call-1",
        "assistant_step_id": "a1",
        "name": "Read",
    }
    _message(
        db,
        "synthetic",
        3,
        "tool",
        "cancelled",
        turn_id="turn-1",
        metadata={
            **common,
            "synthetic_tool_result": True,
            "synthetic_reason": "user_interruption",
        },
    )
    _message(
        db, "real", 4, "tool", "finished", turn_id="turn-1", metadata=common,
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert [message.message_id for message in projection.messages] == ["u1", "a1", "real"]
    assert projection.sealed_boundary.message_id == "real"


def test_nonportable_reasoning_remains_provenance_not_assistant_prose() -> None:
    from history.projection import render_portable_history
    from history.adapters import to_native_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "go", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "answer",
        turn_id="turn-1",
        metadata={
            "reasoning": "provider-private work",
            "reasoning_provenance": {
                "source_harness": "codex",
                "source_provider": "openai",
                "representation_kind": "opaque",
                "native_reference": "rs_1",
            },
        },
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assistant = projection.messages[1]
    assert assistant.reasoning_text == "provider-private work"
    assert assistant.reasoning_content is None
    assert assistant.reasoning_provenance["native_reference"] == "rs_1"
    assert to_native_history(projection)[1] == {
        "role": "assistant", "content": "answer",
    }


def test_empty_user_content_with_attachment_is_semantic_and_drops_preview_blob() -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(
        db,
        "u1",
        1,
        "user",
        "",
        turn_id="turn-1",
        metadata={
            "attachments": [{
                "file_id": "file-1",
                "filename": "photo.png",
                "mime_type": "image/png",
                "preview_url": "blob:must-not-port",
            }],
        },
    )
    _message(db, "a1", 2, "assistant", "I can see it", turn_id="turn-1")
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert projection.messages[0].content == ""
    assert list(projection.messages[0].artifact_references[0]) == [
        "file_id", "filename", "mime_type",
    ]
    assert projection.messages[0].artifact_references == ({
        "file_id": "file-1",
        "filename": "photo.png",
        "mime_type": "image/png",
    },)


def test_explicit_prebinding_legacy_segment_is_portable_without_fabricated_turn() -> None:
    from history.folds import (
        checkpoint_fold_units,
        fragment_checkpoint_fold_unit,
    )
    from history.projection import render_portable_history

    db = _db()
    legacy = {"turn_unbound_reason": "pre_turn_binding_migration"}
    _message(db, "u1", 1, "user", "old request", turn_id=None, metadata=legacy)
    _message(db, "a1", 2, "assistant", "old answer", turn_id=None, metadata=legacy)
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert len(projection.turns) == 1
    assert projection.turns[0].turn_id is None
    assert projection.turns[0].legacy_reason == "pre_turn_binding_migration"
    assert projection.turns[0].status == "legacy"
    units = checkpoint_fold_units(projection)
    assert [(unit.status, unit.legacy_reason) for unit in units] == [
        ("legacy", "pre_turn_binding_migration")
    ]
    fragments = fragment_checkpoint_fold_unit(units[0], max_payload_bytes=4)
    assert {
        (fragment.turn_status, fragment.turn_legacy_reason)
        for fragment in fragments
    } == {("legacy", "pre_turn_binding_migration")}


def test_prebinding_legacy_preserves_assistant_first_and_user_only_segments() -> None:
    from history.projection import render_portable_history

    db = _db()
    legacy = {"turn_unbound_reason": "pre_turn_binding_migration"}
    _message(db, "a0", 1, "assistant", "task started externally", turn_id=None,
             metadata=legacy)
    _message(db, "u1", 2, "user", "follow up", turn_id=None, metadata=legacy)
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert [[message.role for message in turn.messages] for turn in projection.turns] == [
        ["assistant"],
        ["user"],
    ]


def test_bound_turn_must_begin_with_its_accepted_user_input() -> None:
    from history.types import PortableHistoryError
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "a1", 1, "assistant", "premature", turn_id="turn-1")
    _message(db, "u1", 2, "user", "late input", turn_id="turn-1")
    db.commit()

    with pytest.raises(PortableHistoryError, match="does not begin with user input"):
        render_portable_history("thread-1", db=db)


def test_prebinding_legacy_rejects_ownerless_leading_tool_result() -> None:
    from history.types import PortableHistoryError
    from history.projection import render_portable_history

    db = _db()
    _message(
        db,
        "r0",
        1,
        "tool",
        "orphan",
        turn_id=None,
        metadata={
            "turn_unbound_reason": "pre_turn_binding_migration",
            "tool_call_id": "missing-call",
        },
    )
    db.commit()

    with pytest.raises(PortableHistoryError, match="ownerless tool result"):
        render_portable_history("thread-1", db=db)


def test_new_unbound_semantic_row_projects_and_reports(monkeypatch) -> None:
    from history import messages
    from history.projection import render_portable_history

    reported: list[tuple] = []
    monkeypatch.setattr(messages.logger, "warning", lambda *args: reported.append(args))
    db = _db()
    _message(db, "u1", 1, "user", "unowned", turn_id=None)
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert projection.turns[0].legacy_reason == "unusable_turn_provenance"
    assert projection.turns[0].messages[0].content == "unowned"
    assert reported[0][:2] == (
        "portable history: message {} has unusable Turn provenance ({}); "
        "projecting it as a row bound to no Turn",
        "u1",
    )
    assert "no Turn provenance" in str(reported[0][2])


def test_unusable_non_tool_provenance_between_turn_rows_stays_in_that_turn() -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "request", turn_id="turn-1")
    _message(db, "bad", 2, "assistant", "unusable", turn_id=None)
    _message(db, "a1", 3, "assistant", "answer", turn_id="turn-1")
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert [
        (turn.turn_id, [message.message_id for message in turn.messages])
        for turn in projection.turns
    ] == [("turn-1", ["u1", "bad", "a1"])]


@pytest.mark.parametrize(
    ("role", "metadata"),
    [
        ("user", None),
        ("user", {"turn_unbound_reason": "legacy_import"}),
        ("assistant", {"turn_unbound_reason": "legacy_import"}),
    ],
)
def test_unbound_row_between_turn_rows_stays_in_that_turn(role, metadata) -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "request", turn_id="turn-1")
    _message(db, "bad", 2, role, "unbound", turn_id=None, metadata=metadata)
    _message(db, "a1", 3, "assistant", "answer", turn_id="turn-1")
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert [
        (turn.turn_id, [message.message_id for message in turn.messages])
        for turn in projection.turns
    ] == [("turn-1", ["u1", "bad", "a1"])]


def test_unusable_tool_provenance_stays_strict() -> None:
    from history.types import PortableHistoryError
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(
        db,
        "r1",
        1,
        "tool",
        "result",
        turn_id="turn-1",
        metadata={"turn_unbound_reason": "legacy_import"},
    )
    db.commit()

    with pytest.raises(PortableHistoryError, match="conflicting Turn provenance"):
        render_portable_history("thread-1", db=db)


def test_reader_still_projects_a_row_carrying_the_unusable_label() -> None:
    from history.projection import render_portable_history
    from infra.turn_provenance import UNUSABLE_TURN_PROVENANCE_REASON

    db = _db()
    _message(
        db,
        "u1",
        1,
        "user",
        "labelled",
        turn_id=None,
        metadata={"turn_unbound_reason": UNUSABLE_TURN_PROVENANCE_REASON},
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert projection.turns[0].legacy_reason == UNUSABLE_TURN_PROVENANCE_REASON
    assert projection.turns[0].messages[0].content == "labelled"


@pytest.mark.parametrize(
    "metadata",
    [{"turn_unbound_reason": "legacy_import"}, {"turn_id": 42}],
)
def test_turn_provenance_mode_must_be_exclusive_and_valid(metadata) -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(
        db,
        "u1",
        1,
        "user",
        "bad provenance",
        turn_id="turn-1" if "turn_id" not in metadata else None,
        metadata=metadata,
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)

    assert projection.turns[0].legacy_reason == "unusable_turn_provenance"


def test_cross_turn_tool_result_cannot_attach_to_another_turn_step() -> None:
    from history.types import PortableHistoryError
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _turn(db, "turn-2", "completed")
    _message(db, "u1", 1, "user", "one", turn_id="turn-1")
    _message(
        db,
        "a1",
        2,
        "assistant",
        "",
        turn_id="turn-1",
        metadata={
            "llm_tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        },
    )
    _message(
        db,
        "r0",
        3,
        "tool",
        "right owner",
        turn_id="turn-1",
        metadata={
            "tool_call_id": "call-1",
            "assistant_step_id": "a1",
            "name": "Read",
        },
    )
    _message(db, "u2", 4, "user", "two", turn_id="turn-2")
    _message(
        db,
        "r1",
        5,
        "tool",
        "wrong owner",
        turn_id="turn-2",
        metadata={
            "tool_call_id": "call-1",
            "assistant_step_id": "a1",
            "name": "Read",
        },
    )
    _message(db, "a2", 6, "assistant", "two answer", turn_id="turn-2")
    db.commit()

    with pytest.raises(PortableHistoryError, match="missing Assistant Step"):
        render_portable_history("thread-1", db=db)


def test_retention_stub_inside_requested_tail_is_not_exact() -> None:
    from history.types import PortableHistoryUnavailableError
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _turn(db, "turn-2", "completed")
    _message(db, "u1", 1, "user", "one", turn_id="turn-1")
    _message(db, "a1", 2, "assistant", "one answer", turn_id="turn-1")
    _message(db, "u2", 3, "user", "two", turn_id="turn-2")
    _message(
        db,
        "a2",
        4,
        "assistant",
        "",
        turn_id="turn-2",
        metadata={
            "llm_tool_calls": [{
                "id": "call-2",
                "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        },
    )
    _message(
        db,
        "r2",
        5,
        "tool",
        "[tool output trimmed after 30d retention — 10 chars, 1 lines]",
        turn_id="turn-2",
        metadata={
            "tool_call_id": "call-2",
            "assistant_step_id": "a2",
            "name": "Read",
        },
    )
    db.commit()
    full = render_portable_history("thread-1", db=db)

    with pytest.raises(PortableHistoryUnavailableError, match="retained"):
        render_portable_history(
            "thread-1", db=db, start_exclusive=full.turns[0].boundary,
        )


def test_inflight_user_is_excluded_by_identity_not_repeated_content() -> None:
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "same words", turn_id="turn-1")
    _message(db, "a1", 2, "assistant", "answer", turn_id="turn-1")
    _turn(db, "future-turn", "running")
    _message(
        db,
        "current-u",
        None,
        "user",
        "same words",
        turn_id="future-turn",
        delivery_state="queued",
    )
    db.commit()

    projection = render_portable_history(
        "thread-1", db=db, exclude_message_id="current-u",
    )

    assert [message.message_id for message in projection.messages] == ["u1", "a1"]
    assert projection.sealed_boundary.message_id == "a1"


def test_identity_exclusion_cannot_hide_completed_user_only_turn() -> None:
    from history.types import PortableHistoryError
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(db, "u1", 1, "user", "accepted input", turn_id="turn-1")
    db.commit()

    with pytest.raises(PortableHistoryError, match="canonical in-flight Turn input"):
        render_portable_history("thread-1", db=db, exclude_message_id="u1")


def test_native_translation_reconstructs_agent_send_envelope_from_typed_source() -> None:
    from history.projection import render_portable_history
    from history.adapters import to_native_history

    db = _db()
    _turn(db, "turn-1", "completed")
    _message(
        db,
        "u1",
        1,
        "user",
        "please inspect",
        turn_id="turn-1",
        metadata={
            "sender_kind": "agent",
            "from_thread": "source-thread",
            "from_title": "Research",
            "scope": "project-a",
        },
    )
    _message(db, "a1", 2, "assistant", "done", turn_id="turn-1")
    db.commit()

    history = to_native_history(render_portable_history("thread-1", db=db))

    assert history[0]["content"] == (
        '[AGENT-SEND — from "Research" (thread source-thread), scope project-a]\n'
        "please inspect\n"
        '[To reply, call delegate_send(thread_id="source-thread", message="<your reply>")]'
    )
