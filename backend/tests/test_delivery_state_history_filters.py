from __future__ import annotations

import sqlite3


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            history_epoch INTEGER NOT NULL DEFAULT 0,
            project_id TEXT
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'delivered',
            delivery_error TEXT,
            active_turn_id TEXT,
            seq INTEGER
        );
        INSERT INTO threads (id,history_epoch) VALUES ('thread-1', 3);
        INSERT INTO messages
            (id,thread_id,role,content,created_at,delivery_state,seq)
        VALUES
            ('delivered','thread-1','user','provider sees this','t1','delivered',1),
            ('queued','thread-1','user','provider must not see this','t2','queued',NULL),
            ('failed','thread-1','user','provider must not see this either','t3','undelivered',NULL);
        """
    )
    return conn


def test_provider_history_projections_ignore_pending_and_failed_messages():
    """Queued/undelivered rows stay durable UI records, never model history."""
    from core.context_admission import check_context_preflight
    from core.context_preview import _message_count
    from history.source import _validated_rows
    from core.thread_history import rebuild_messages_from_connection

    conn = _db()
    try:
        rebuilt = rebuild_messages_from_connection(
            thread_id="thread-1", memory_window=100, db=conn,
        )
        assert rebuilt == [{"role": "user", "content": "provider sees this"}]

        _epoch, rows = _validated_rows(conn, "thread-1")
        assert [row["id"] for row in rows] == ["delivered"]
        assert _message_count("thread-1", conn) == 1

        result = check_context_preflight(
            thread_id="thread-1",
            model="test/model",
            current_user_content="next",
            db=conn,
            model_context_window=100_000,
        )
        assert result.history_tokens < 100
    finally:
        conn.close()


def test_rebuilt_history_uses_delivery_sequence_not_queued_creation_time():
    """A mid-turn thought must follow the completed tool/assistant step it saw."""
    from core.thread_history import rebuild_messages_from_connection

    conn = _db()
    try:
        conn.executemany(
            "INSERT INTO messages "
            "(id,thread_id,role,content,created_at,delivery_state,seq) "
            "VALUES (?,?,?,?,?,'delivered',?)",
            [
                ("step-user", "thread-1", "user", "begin", "t10", 2),
                ("step-assistant", "thread-1", "assistant", "used a tool", "t20", 3),
                # This thought was typed during the tool batch (t15), but was
                # deliberately delivered only at the following safe boundary.
                ("safe-boundary-user", "thread-1", "user", "now change course", "t15", 4),
            ],
        )

        history = rebuild_messages_from_connection("thread-1", 100, db=conn)

        assert [(row["role"], row["content"]) for row in history] == [
            ("user", "provider sees this"),
            ("user", "begin"),
            ("assistant", "used a tool"),
            ("user", "now change course"),
        ]
    finally:
        conn.close()
