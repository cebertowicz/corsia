from __future__ import annotations

import sqlite3

from infra.db_fts import _init_messages_fts


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            role TEXT NOT NULL
        );

        -- _init_fts creates every fresh-schema index, which since schema 7
        -- includes the checkpoint narrative index. Only the columns that
        -- index carries are needed here.
        CREATE TABLE checkpoints (
            generation_order INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_result TEXT,
            continuity_snapshot_json TEXT
        );
        """
    )
    return conn


def _match_count(conn: sqlite3.Connection, term: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
            (term,),
        ).fetchone()[0]
    )


def test_messages_fts_does_not_duplicate_message_content() -> None:
    conn = _db()
    _init_messages_fts(conn)
    conn.execute(
        "INSERT INTO messages (id, content, role) VALUES ('m1', 'needle', 'assistant')"
    )
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='messages_fts_content'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT content, role FROM messages_fts WHERE rowid = "
        "(SELECT rowid FROM messages WHERE id = 'm1')"
    ).fetchone() == ("needle", "assistant")
    assert conn.execute(
        "SELECT bm25(messages_fts) FROM messages_fts "
        "WHERE messages_fts MATCH 'needle'"
    ).fetchone()[0] is not None


def test_message_content_and_role_updates_refresh_fts_row() -> None:
    conn = _db()
    _init_messages_fts(conn)
    conn.execute(
        "INSERT INTO messages (id, content, role) VALUES ('m1', 'beforeterm', 'tool')"
    )
    conn.commit()

    conn.execute(
        "UPDATE messages SET content = 'afterterm', role = 'assistant' WHERE id = 'm1'"
    )
    conn.commit()

    assert _match_count(conn, "beforeterm") == 0
    assert _match_count(conn, "afterterm") == 1
    assert _match_count(conn, "assistant") == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='trigger' AND name='messages_fts_update'"
    ).fetchone()[0] == 1


def test_message_delete_removes_external_fts_tokens() -> None:
    conn = _db()
    _init_messages_fts(conn)
    conn.execute(
        "INSERT INTO messages (id, content, role) "
        "VALUES ('m1', 'deletedterm', 'assistant')"
    )
    conn.commit()

    conn.execute("DELETE FROM messages WHERE id = 'm1'")
    conn.commit()

    assert _match_count(conn, "deletedterm") == 0
    conn.execute(
        "INSERT INTO messages_fts(messages_fts, rank) "
        "VALUES('integrity-check', 1)"
    )


def test_message_fts_rebuild_restores_the_derived_index() -> None:
    conn = _db()
    _init_messages_fts(conn)
    conn.execute(
        "INSERT INTO messages (id, content, role) "
        "VALUES ('m1', 'recoveryterm', 'assistant')"
    )
    rowid = conn.execute(
        "SELECT rowid FROM messages WHERE id = 'm1'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO messages_fts(messages_fts, rowid, content, role) "
        "VALUES ('delete', ?, 'recoveryterm', 'assistant')",
        (rowid,),
    )

    assert _match_count(conn, "recoveryterm") == 0

    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

    assert _match_count(conn, "recoveryterm") == 1
    conn.execute(
        "INSERT INTO messages_fts(messages_fts, rank) "
        "VALUES('integrity-check', 1)"
    )
