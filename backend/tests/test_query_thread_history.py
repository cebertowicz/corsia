from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3

import pytest

from tools.query_helpers import format_history_results, search_messages_fts
from tools.query_projects import GetThreadHistoryTool, _history_line


@dataclass
class _Result:
    one: dict | None = None
    all: list[dict] | None = None

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all or []


class _HistoryDb:
    def __init__(self, title: str | None, rows: list[dict]) -> None:
        self.title = title
        self.rows = rows

    def execute(self, sql: str, _params):
        if "SELECT title FROM threads" in sql:
            row = None if self.title is None else {"title": self.title}
            return _Result(one=row)
        return _Result(all=self.rows)

    def close(self) -> None:
        pass


async def _run_inline(fn):
    return fn()


def _message(*, seq: int = 1) -> dict:
    return {
        "seq": seq,
        "role": "user",
        "content": "hello",
        "metadata": None,
        "created_at": "2026-09-06T04:00:00Z",
    }


def _history_row(*, role: str, content: str, seq: int = 7) -> dict:
    return {
        "seq": seq,
        "role": role,
        "content": content,
        "metadata": None,
        "created_at": "2026-09-06T04:00:00Z",
    }


@pytest.mark.parametrize("role", ["user", "assistant", "system"])
def test_history_line_marks_clipped_messages_in_preview(role):
    content = "m" * 501

    line = _history_line(_history_row(role=role, content=content), "preview")

    assert line.endswith(
        content[:500]
        + "… [trimmed, 501 chars total — for this one message "
        "call again with from_seq=7, to_seq=7, tool_output='full']"
    )


def test_history_line_marks_clipped_messages_in_omit_mode():
    content = "m" * 501

    line = _history_line(_history_row(role="user", content=content), "omit")

    assert line.endswith(
        content[:500]
        + "… [trimmed, 501 chars total — for this one message "
        "call again with from_seq=7, to_seq=7, tool_output='full']"
    )


@pytest.mark.parametrize("role", ["user", "assistant", "system"])
def test_history_line_returns_whole_messages_in_full(role):
    content = "m" * 501

    line = _history_line(_history_row(role=role, content=content), "full")

    assert line.endswith(content)
    assert "[trimmed," not in line


def test_history_line_preserves_tool_preview():
    content = "t" * 241

    line = _history_line(_history_row(role="tool", content=content), "preview")

    assert line.endswith(
        content[:240]
        + "… [trimmed, 241 chars total — for this one result "
        "call again with from_seq=7, to_seq=7, tool_output='full']"
    )


@pytest.mark.asyncio
async def test_get_thread_history_omits_tool_rows(monkeypatch):
    import infra.database
    import tools.query_projects as query_projects

    rows = [
        _history_row(role="user", content="visible", seq=1),
        _history_row(role="tool", content="hidden", seq=2),
    ]

    class _OmitHistoryDb(_HistoryDb):
        def execute(self, sql: str, params):
            if "role <> 'tool'" in sql:
                return _Result(all=[row for row in self.rows if row["role"] != "tool"])
            return super().execute(sql, params)

    monkeypatch.setattr(query_projects, "run_in_db", _run_inline)
    monkeypatch.setattr(infra.database, "get_db", lambda: _OmitHistoryDb("Planning", rows))

    result = await GetThreadHistoryTool().execute("thread-1", tool_output="omit")

    assert "visible" in result
    assert "hidden" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "rows", "expected"),
    [
        ("Planning", [_message()], 'Thread "Planning" (thread-1) — 1 messages:'),
        ("Planning", [], "No messages found in thread 'thread-1'"),
        (None, [], "Error: Thread 'thread-1' not found"),
    ],
)
async def test_get_thread_history_tool_uses_one_thread_vocabulary(
    monkeypatch, title, rows, expected
):
    import infra.database
    import tools.query_projects as query_projects

    monkeypatch.setattr(query_projects, "run_in_db", _run_inline)
    monkeypatch.setattr(infra.database, "get_db", lambda: _HistoryDb(title, rows))

    tool = GetThreadHistoryTool()
    assert tool.name == "get_thread_history"
    assert "thread" in tool.description.lower()
    assert expected in await tool.execute("thread-1")


def test_search_history_uses_durable_sender_schema():
    from tools.query_helpers import SENDER_CLASSES
    from tools.query_search import SearchHistoryTool

    tool = SearchHistoryTool()
    parameters = tool.parameters["properties"]

    assert "role" not in parameters
    assert parameters["sender"]["enum"] == list(SENDER_CLASSES)
    assert all(sender in tool.description for sender in SENDER_CLASSES)


def test_history_results_label_thread_identity_and_title():
    rows = [
        {
            "content": "alpha",
            "title": "Planning",
            "bm25_score": None,
            "thread_id": "thread-1",
            "created_at": "2026-09-06T04:00:00Z",
            "role": "user",
            "sender": "human",
        }
    ]
    result = format_history_results(rows, "plan", "thread-1", "human")

    assert result == (
        "Found 1 message(s) matching 'plan' in thread thread-1 from sender human:\n\n"
        "[2026-09-06T04:00] HUMAN | thread_id: thread-1 | \"Planning\":\n"
        "  alpha"
    )
    assert format_history_results([], "plan", "thread-1", "human") == (
        "No messages found matching 'plan' in thread thread-1 from sender human"
    )


@pytest.mark.parametrize("with_fts", [True, False])
def test_message_search_returns_thread_identity_and_title(with_fts):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
    db.execute(
        "CREATE TABLE messages (id TEXT, thread_id TEXT, role TEXT, content TEXT, "
        "metadata TEXT, created_at TEXT, delivery_state TEXT)"
    )
    db.execute("INSERT INTO threads VALUES ('thread-1', 'Planning')")
    db.execute(
        "INSERT INTO messages VALUES "
        "('message-1', 'thread-1', 'user', 'alpha', NULL, '2026-09-06T04:00:00Z', 'delivered')"
    )
    if with_fts:
        db.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
        db.execute("INSERT INTO messages_fts(rowid, content) VALUES (1, 'alpha')")

    rows = search_messages_fts(db, "alpha")

    assert len(rows) == 1
    assert rows[0]["thread_id"] == "thread-1"
    assert rows[0]["title"] == "Planning"
    assert rows[0]["sender"] == "human"
    db.close()


@pytest.mark.parametrize("with_fts", [True, False])
def test_message_search_filters_and_labels_durable_sender_class(with_fts):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
    db.execute(
        "CREATE TABLE messages (id TEXT, thread_id TEXT, role TEXT, content TEXT, "
        "metadata TEXT, created_at TEXT, delivery_state TEXT)"
    )
    db.execute("INSERT INTO threads VALUES ('thread-1', 'Planning')")
    messages = [
        ("human", "user", {}),
        ("agent", "user", {"sender_kind": "agent"}),
        ("result", "user", {"sender_kind": "agent", "completion": True}),
        ("event", "system", {"routed_event": "github.pull_request"}),
        # The v20 migration shape: its specific plan event, not origin, identifies a wake.
        ("wake", "system", {
            "type": "context_event", "event_disposition": "wake",
            "event_type": "plan.child_returned", "origin": "plan",
            "plan_id": "p1", "node_id": "n1",
        }),
        ("decision-answer", "system", {
            "type": "context_event", "event_disposition": "wake",
            "event_type": "plan.decision_answered", "origin": "plan",
            "plan_id": "p1", "decision_id": "d1",
        }),
        ("assistant-plan", "assistant", {"origin": "plan", "plan_node_id": "n1"}),
        ("receipt", "system", {
            "type": "activity_receipt", "event_type": "usage.recording_failed",
            "origin": "plan", "plan_node_id": "n1",
        }),
        ("agent-assistant", "assistant", {"sender_kind": "agent"}),
        ("event-assistant", "assistant", {"routed_event": "github.pull_request"}),
        ("digest", "system", {"type": "context_event", "event_disposition": "passive"}),
    ]
    db.executemany(
        "INSERT INTO messages VALUES (?, 'thread-1', ?, 'alpha', ?, '2026-09-06T04:00:00Z', 'delivered')",
        [(message_id, role, json.dumps(metadata)) for message_id, role, metadata in messages],
    )
    if with_fts:
        db.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
        for rowid in range(1, len(messages) + 1):
            db.execute("INSERT INTO messages_fts(rowid, content) VALUES (?, 'alpha')", (rowid,))

    by_sender = {
        sender: search_messages_fts(db, "alpha", sender=sender)[0]["id"]
        for sender in (
            "human", "agent_send", "subagent_result", "routed_event", "plan_wake",
            "system_digest", "assistant", "system",
        )
    }

    assert by_sender == {
        "human": "human",
        "agent_send": "agent",
        "subagent_result": "result",
        "routed_event": "event",
        "plan_wake": "wake",
        "system_digest": "digest",
        "assistant": "assistant-plan",
        "system": "receipt",
    }
    assert {
        row["id"] for row in search_messages_fts(db, "alpha", sender="plan_wake")
    } == {"wake", "decision-answer"}
    assert "SUBAGENT RESULT" in format_history_results(
        search_messages_fts(db, "alpha", sender="subagent_result"),
        "alpha",
        "thread-1",
        "subagent_result",
    )
    db.close()
