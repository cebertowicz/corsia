from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from infra.schema import ARTIFACT_IDENTITY_SCHEMA_SQL


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            history_epoch INTEGER NOT NULL DEFAULT 0,
            project_id TEXT,
            spawned_by TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            seq INTEGER NOT NULL,
            active_turn_id TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
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
    db.execute("INSERT INTO threads (id, history_epoch) VALUES ('thread-1', 4)")
    return db


def _turn(db: sqlite3.Connection, turn_id: str) -> None:
    db.execute(
        "INSERT INTO active_turns "
        "(id, thread_id, status, started_at, ended_at) "
        "VALUES (?, 'thread-1', 'completed', 'start', 'end')",
        (turn_id,),
    )


def _message(
    db: sqlite3.Connection,
    message_id: str,
    seq: int,
    role: str,
    content: str,
    turn_id: str,
    metadata: dict | None = None,
) -> None:
    meta = metadata or {}
    db.execute(
        "INSERT INTO messages "
        "(id, thread_id, role, content, metadata, created_at, seq,active_turn_id) "
        "VALUES (?, 'thread-1', ?, ?, ?, ?, ?, ?)",
        (message_id, role, content, json.dumps(meta), f"time-{seq}", seq, turn_id),
    )


def _seed(db: sqlite3.Connection) -> None:
    _turn(db, "turn-1")
    _turn(db, "turn-2")
    _message(
        db,
        "user-1",
        1,
        "user",
        "inspect the artifact",
        "turn-1",
        {
            "attachments": [{"artifact_id": "artifact-1", "path": "docs/a.md"}],
            "sender_kind": "agent",
            "from_thread": "source-thread",
            "from_title": "Research",
        },
    )
    _message(
        db,
        "assistant-step-1",
        2,
        "assistant",
        "checking",
        "turn-1",
        {
            "reasoning": "portable reasoning",
            "reasoning_provenance": {
                "source_harness": "native",
                "source_provider": "omlx",
                "representation_kind": "reasoning_content",
            },
            "llm_tool_calls": [
                {
                    "id": "call-error",
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"path":"a"}'},
                },
                {
                    "id": "call-cancelled",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": "{}"},
                },
            ],
        },
    )
    _message(
        db,
        "result-error",
        3,
        "tool",
        "permission denied",
        "turn-1",
        {
            "tool_call_id": "call-error",
            "assistant_step_id": "assistant-step-1",
            "name": "Read",
            "is_error": True,
        },
    )
    _message(
        db,
        "result-cancelled",
        4,
        "tool",
        "This tool call was cancelled by user interruption.",
        "turn-1",
        {
            "tool_call_id": "call-cancelled",
            "assistant_step_id": "assistant-step-1",
            "name": "Bash",
            "synthetic_tool_result": True,
            "synthetic_reason": "user_interruption",
        },
    )
    _message(db, "assistant-step-2", 5, "assistant", "done", "turn-1")
    _message(db, "user-2", 6, "user", "continue", "turn-2")
    _message(
        db,
        "assistant-step-3",
        7,
        "assistant",
        "second answer",
        "turn-2",
        {
            "reasoning": "signed Claude thinking",
            "reasoning_provenance": {
                "source_harness": "claude-code",
                "source_provider": "anthropic",
                "representation_kind": "anthropic_thinking",
                "native_signature": "signature-1",
            },
        },
    )
    db.commit()


def _signed_reasoning_projection(
    *,
    catalog_label: str,
    source_effective_model: str | None = "claude-fable-5",
):
    from history.projection import render_portable_history

    db = _db()
    _turn(db, "turn-signed")
    _message(db, "user-signed", 1, "user", "continue", "turn-signed")
    provenance = {
        "source_harness": "claude-code",
        "source_provider": "anthropic",
        "representation_kind": "anthropic_thinking",
        "native_signatures": ["sig-first", "sig-second"],
        "native_blocks": [
            {"type": "thinking", "thinking": "first", "signature": "sig-first"},
            {"type": "redacted_thinking", "data": "redacted-payload"},
            {"type": "thinking", "thinking": "second", "signature": "sig-second"},
        ],
    }
    if source_effective_model is not None:
        provenance["source_effective_model"] = source_effective_model
    _message(
        db,
        "assistant-signed",
        2,
        "assistant",
        "answer",
        "turn-signed",
        {
            "model": catalog_label,
            "reasoning": "first\nsecond",
            "reasoning_provenance": provenance,
        },
    )
    db.commit()
    return db, render_portable_history("thread-1", db=db)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_cc_adapter_preserves_empty_canonical_rows_in_order() -> None:
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import cc_snapshot_history

    db = _db()
    _turn(db, "turn-empty")
    _message(db, "user-empty", 1, "user", "", "turn-empty")
    _message(db, "assistant-empty", 2, "assistant", "", "turn-empty")
    db.commit()

    projection = render_portable_history("thread-1", db=db)
    history = cc_snapshot_history(projection, cc_session_id="cc-empty")

    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[0]["content"] == "(empty)"
    assert history[1]["content"] == [{"type": "text", "text": "(empty)"}]


def test_cc_historical_image_metadata_remains_text_only() -> None:
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import cc_snapshot_history

    db = _db()
    _turn(db, "turn-image")
    _message(
        db,
        "user-image",
        1,
        "user",
        "remember the visual conclusion",
        "turn-image",
        {
            "attachments": [{
                "file_id": "historical-image",
                "filename": "visual.png",
                "mime_type": "image/png",
                "_corsia_validated_image_sha256": "a" * 64,
            }],
        },
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)
    history = cc_snapshot_history(projection, cc_session_id="cc-history-image")

    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert isinstance(history[0]["content"], str)
    assert history[0]["content"].startswith("remember the visual conclusion")
    assert "[CORSIA artifact references]" in history[0]["content"]
    rendered = json.dumps(history)
    assert '"type": "image"' not in rendered
    assert '"source"' not in rendered
    assert "_corsia_validated_image_sha256" not in rendered


@pytest.mark.parametrize("arguments", ["{malformed", "[]"])
def test_cc_adapter_fails_closed_on_non_object_tool_arguments(arguments: str) -> None:
    from history.types import PortableHistoryError
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import cc_snapshot_history

    db = _db()
    _turn(db, "turn-tool")
    _message(db, "user", 1, "user", "run", "turn-tool")
    _message(
        db,
        "assistant",
        2,
        "assistant",
        "",
        "turn-tool",
        {"llm_tool_calls": [{
            "id": "call-1",
            "type": "function",
            "function": {"name": "Read", "arguments": arguments},
        }]},
    )
    _message(
        db,
        "result",
        3,
        "tool",
        "done",
        "turn-tool",
        {"assistant_step_id": "assistant", "tool_call_id": "call-1"},
    )
    db.commit()

    projection = render_portable_history("thread-1", db=db)
    with pytest.raises(PortableHistoryError, match="object tool arguments"):
        cc_snapshot_history(projection, cc_session_id="cc-malformed")


def test_cc_adapter_preserves_portable_typed_semantics_and_boundary(
    monkeypatch, tmp_path,
) -> None:
    from core.message_history import frame_agent_envelope
    from history.projection import render_portable_history
    from history.adapters import to_native_history
    from subagents.cc_context_reconstruction import (
        cc_snapshot_history,
        reconstruct_cc_transcript_from_corsia,
    )
    db = _db()
    _seed(db)
    projection = render_portable_history("thread-1", db=db)
    path = tmp_path / "claude" / "projects" / "-workspace" / "cc-session.jsonl"
    fresh_render = cc_snapshot_history(
        projection,
        cc_session_id="cc-session",
    )

    rebuilt = reconstruct_cc_transcript_from_corsia(
        projection=projection,
        cc_session_id="cc-session",
        path=str(path),
        working_dir="/workspace",
    )

    rows = _rows(path)
    first_user = rows[0]["message"]["content"]
    assert first_user.startswith(frame_agent_envelope(
        "inspect the artifact",
        {
            "sender_kind": "agent",
            "from_thread": "source-thread",
            "from_title": "Research",
        },
    ))
    assert "artifact-1" in first_user
    assert "docs/a.md" in first_user
    assert "source-thread" in first_user

    blocks = rows[1]["message"]["content"]
    assert blocks[0] == {"type": "text", "text": "checking"}
    assert [block["id"] for block in blocks[1:]] == [
        "call-error",
        "call-cancelled",
    ]
    assert all(block.get("type") != "thinking" for block in blocks)
    assert "portable reasoning" not in path.read_text()
    native_history = to_native_history(projection)
    assert native_history[1]["reasoning_content"] == "portable reasoning"
    signed_blocks = rows[-1]["message"]["content"]
    assert signed_blocks == [{"type": "text", "text": "second answer"}]

    error_result = rows[2]["message"]["content"][0]
    cancelled_result = rows[3]["message"]["content"][0]
    assert error_result["is_error"] is True
    assert cancelled_result["is_error"] is True
    assert "cancelled by user interruption" in cancelled_result["content"][0]["text"]

    assert rebuilt.format_version == projection.format_version
    assert rebuilt.start_exclusive == projection.start_exclusive
    assert rebuilt.sealed_boundary == projection.sealed_boundary
    assert rebuilt.full_raw_replay_available is True
    replay_render = [row["message"] for row in rows]
    assert replay_render == fresh_render
    assert list(rebuilt.snapshot_history) == fresh_render

    from subagents import cc_context_reconstruction

    monkeypatch.setattr(
        cc_context_reconstruction,
        "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            installed=None, project_cwd="/workspace",
            sources=[],
            agent_facts={},
        ),
    )
    frame = cc_context_reconstruction.build_cc_user_frame_reconstruction(
        thread_id="thread-1",
        user_text="next input",
        catalog_model_id="claude-code/opus",
        model_alias="opus",
        working_dir="/workspace",
        project_id="project-1",
        is_subagent=False,
        history=list(rebuilt.snapshot_history),
    )
    assert frame.snapshot.frozen_prompt_material["history"] == [
        row["message"] for row in rows
    ]


@pytest.mark.parametrize(
    "catalog_label",
    ["claude-code/fable", "claude-code/custom-alias-for-fable"],
)
def test_exact_effective_model_replays_complete_signed_blocks_across_aliases(
    catalog_label,
) -> None:
    from history.adapters import to_native_history
    from subagents.cc_context_reconstruction import cc_snapshot_history

    _db_handle, projection = _signed_reasoning_projection(
        catalog_label=catalog_label,
    )
    canonical_before = json.loads(json.dumps(
        projection.messages[-1].reasoning_provenance,
    ))

    cc_history = cc_snapshot_history(
        projection,
        cc_session_id="cc-exact-model",
        destination_provider="anthropic",
        destination_effective_model="claude-fable-5",
    )
    native_history = to_native_history(
        projection,
        destination_provider="anthropic",
        destination_effective_model="claude-fable-5",
    )

    expected_blocks = [
        {"type": "thinking", "thinking": "first", "signature": "sig-first"},
        {"type": "redacted_thinking", "data": "redacted-payload"},
        {"type": "thinking", "thinking": "second", "signature": "sig-second"},
        {"type": "text", "text": "answer"},
    ]
    assert cc_history[-1]["content"] == expected_blocks
    assert native_history[-1]["content"] == expected_blocks
    assert projection.messages[-1].reasoning_provenance == canonical_before


@pytest.mark.parametrize(
    "destination_effective_model",
    ["claude-opus-5", "claude-sonnet-5", None],
)
def test_same_catalog_alias_cannot_replay_mismatched_or_unknown_signed_blocks(
    destination_effective_model,
) -> None:
    from history.adapters import to_native_history
    from subagents.cc_context_reconstruction import cc_snapshot_history

    _db_handle, projection = _signed_reasoning_projection(
        catalog_label="claude-code/fable",
    )
    canonical_before = json.loads(json.dumps(
        projection.messages[-1].reasoning_provenance,
    ))

    cc_history = cc_snapshot_history(
        projection,
        cc_session_id="cc-mismatch-model",
        destination_provider="anthropic",
        destination_effective_model=destination_effective_model,
    )
    native_history = to_native_history(
        projection,
        destination_provider="anthropic",
        destination_effective_model=destination_effective_model,
    )

    assert cc_history[-1]["content"] == [{"type": "text", "text": "answer"}]
    assert native_history[-1]["content"] == "answer"
    assert projection.messages[-1].reasoning_provenance == canonical_before


def test_unknown_source_effective_model_is_not_replayed() -> None:
    from history.adapters import to_native_history
    from subagents.cc_context_reconstruction import cc_snapshot_history

    _db_handle, projection = _signed_reasoning_projection(
        catalog_label="claude-code/fable",
        source_effective_model=None,
    )

    cc_history = cc_snapshot_history(
        projection,
        cc_session_id="cc-unknown-source",
        destination_provider="anthropic",
        destination_effective_model="claude-fable-5",
    )
    native_history = to_native_history(
        projection,
        destination_provider="anthropic",
        destination_effective_model="claude-fable-5",
    )

    assert cc_history[-1]["content"] == [{"type": "text", "text": "answer"}]
    assert native_history[-1]["content"] == "answer"


def test_cc_adapter_uuids_are_stable_when_the_selected_prefix_changes(tmp_path) -> None:
    from history.projection import render_portable_history
    from subagents.cc_context_reconstruction import (
        reconstruct_cc_transcript_from_corsia,
    )

    db = _db()
    _seed(db)
    full = render_portable_history("thread-1", db=db)
    tail = render_portable_history(
        "thread-1", db=db, start_exclusive=full.turns[0].boundary,
    )
    full_path = tmp_path / "full" / "projects" / "-workspace" / "cc-session.jsonl"
    tail_path = tmp_path / "tail" / "projects" / "-workspace" / "cc-session.jsonl"

    reconstruct_cc_transcript_from_corsia(
        projection=full,
        cc_session_id="cc-session",
        path=str(full_path),
        working_dir="/workspace",
    )
    reconstruct_cc_transcript_from_corsia(
        projection=tail,
        cc_session_id="cc-session",
        path=str(tail_path),
        working_dir="/workspace",
    )

    full_rows = _rows(full_path)
    tail_rows = _rows(tail_path)
    full_second_turn = full_rows[-2:]
    assert [row["uuid"] for row in full_second_turn] == [
        row["uuid"] for row in tail_rows
    ]




def test_cc_reconstruction_marks_attached_provider_history_opaque(
    monkeypatch,
) -> None:
    from subagents import cc_context_reconstruction

    monkeypatch.setattr(
        cc_context_reconstruction,
        "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            installed=None, project_cwd="/workspace",
            sources=[],
            agent_facts={},
        ),
    )

    seed = cc_context_reconstruction.build_cc_context_reconstruction(
        thread_id="thread-1",
        base_seed="seed",
        catalog_model_id="claude-code/opus",
        model_alias="opus",
        working_dir="/workspace",
        project_id="project-1",
        is_subagent=False,
        provider_handle_opaque=True,
    )
    frame = cc_context_reconstruction.build_cc_user_frame_reconstruction(
        thread_id="thread-1",
        user_text="continue",
        catalog_model_id="claude-code/opus",
        model_alias="opus",
        working_dir="/workspace",
        project_id="project-1",
        is_subagent=False,
        provider_handle_opaque=True,
    )

    for snapshot in (seed.snapshot, frame.snapshot):
        material = snapshot.frozen_prompt_material
        assert material["history"] == []
        assert material["provider_payload"]["history_source_attribution"] == (
            "provider_handle_opaque"
        )
