"""Assistant Step display lineage remains identical across separate index seeks."""

from __future__ import annotations

import json
import sqlite3

from subagents import assistant_step_display
from subagents.assistant_step_display import load_assistant_step_display


_THREAD_ID = "display-thread"
_STEP_ID = "step"
_CALL_ID = "tool-call"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages ("
        "id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT, "
        "metadata TEXT, created_at TEXT, seq INTEGER)"
    )
    conn.execute(
        "CREATE INDEX idx_messages_tool_lineage ON messages("
        "thread_id, role, json_extract(metadata, '$.assistant_step_id'), "
        "json_extract(metadata, '$.tool_call_id')) "
        "WHERE role='tool' AND json_valid(metadata)"
    )
    return conn


def _insert(
    conn: sqlite3.Connection,
    row_id: str,
    role: str,
    metadata: dict,
    created_at: str,
    *,
    content: str = "",
) -> None:
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, content, metadata, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row_id, _THREAD_ID, role, content, json.dumps(metadata), created_at),
    )


def test_interleaved_lineage_keeps_literal_row_and_display_order() -> None:
    """Fixture output was captured once against the pre-split dev implementation."""
    conn = _db()
    try:
        calls = [
            {"id": "call-a", "function": {"name": "Read", "arguments": "{}"}},
            {"id": "call-b", "function": {"name": "Glob", "arguments": "{}"}},
        ]
        _insert(conn, _STEP_ID, "assistant", {"tool_calls": calls}, "2026-01-01T00:00:10Z")
        _insert(conn, "legacy-b", "tool", {"tool_call_id": "call-b"}, "2026-01-01T00:00:01Z", content="legacy B")
        _insert(conn, "authoritative-a", "tool", {"assistant_step_id": _STEP_ID, "tool_call_id": "call-a"}, "2026-01-01T00:00:02Z", content="authoritative A")
        _insert(conn, "legacy-a", "tool", {"tool_call_id": "call-a"}, "2026-01-01T00:00:02Z", content="legacy A")
        conn.commit()

        assert [row["id"] for row in assistant_step_display._lineage_rows(conn, _THREAD_ID, _STEP_ID)] == ["legacy-b", "authoritative-a", "legacy-a", _STEP_ID]
        assert load_assistant_step_display(_THREAD_ID, _STEP_ID, db=conn) == {
            "id": _STEP_ID, "thread_id": _THREAD_ID, "role": "assistant", "content": "", "created_at": "2026-01-01T00:00:10Z", "seq": None, "activity_weight": 2,
            "metadata": {"tool_calls": [
                {"id": "call-a", "name": "Read", "hint": "Read", "phrase": "Reading", "args": "{}", "object": "", "outcome": "done", "result": "authoritative A"},
                {"id": "call-b", "name": "Glob", "hint": "Glob", "phrase": "Glob", "args": "{}", "object": "", "result": None},
            ]},
        }
    finally:
        conn.close()


def test_step_without_lineage_keeps_literal_display() -> None:
    conn = _db()
    try:
        _insert(conn, _STEP_ID, "assistant", {"tool_calls": []}, "2026-01-01T00:00:10Z")
        conn.commit()
        assert load_assistant_step_display(_THREAD_ID, _STEP_ID, db=conn) == {
            "id": _STEP_ID, "thread_id": _THREAD_ID, "role": "assistant", "content": "", "created_at": "2026-01-01T00:00:10Z", "seq": None, "activity_weight": 1, "metadata": {"tool_calls": []},
        }
    finally:
        conn.close()
