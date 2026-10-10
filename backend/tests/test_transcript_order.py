from __future__ import annotations

import sqlite3


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE threads ("
        "id TEXT PRIMARY KEY, "
        "history_epoch INTEGER NOT NULL, "
        "next_message_seq INTEGER NOT NULL)"
    )
    return connection


def test_draw_transcript_position_atomically_advances_the_session_counter() -> None:
    """A position draw spends exactly the current counter value once."""
    from thread.transcript_order import draw_transcript_position

    connection = _connection()
    try:
        connection.execute(
            "INSERT INTO threads (id,history_epoch,next_message_seq) "
            "VALUES ('thread-1',3,7)"
        )

        first = draw_transcript_position(connection, "thread-1")
        second = draw_transcript_position(connection, "thread-1")

        assert (first, second) == (7, 8)
        assert connection.execute(
            "SELECT next_message_seq FROM threads WHERE id='thread-1'"
        ).fetchone()["next_message_seq"] == 9
    finally:
        connection.close()


def test_destructive_boundary_tracks_turn_order_and_accepted_user_order() -> None:
    """Assistant settlement cannot masquerade as successor user input."""
    from thread.transcript_order import (
        destructive_boundary_is_clear,
        destructive_boundary_version,
    )

    connection = _connection()
    try:
        connection.execute(
            "CREATE TABLE active_turns ("
            "thread_id TEXT,turn_order INTEGER,status TEXT)"
        )
        connection.execute(
            "CREATE TABLE messages ("
            "thread_id TEXT,role TEXT,seq INTEGER,input_order INTEGER)"
        )
        connection.execute(
            "INSERT INTO threads (id,history_epoch,next_message_seq) "
            "VALUES ('thread-1',3,10)"
        )
        connection.executemany(
            "INSERT INTO active_turns (thread_id,turn_order,status) VALUES (?,?,?)",
            [("thread-1", 2, "completed"), ("thread-1", 5, "running")],
        )
        connection.executemany(
            "INSERT INTO messages (thread_id,role,seq,input_order) VALUES (?,?,?,?)",
            [
                ("thread-1", "user", 1, 1),
                ("thread-1", "user", 4, 2),
                ("thread-1", "assistant", 9, None),
                ("thread-1", "user", None, 3),
            ],
        )

        version = destructive_boundary_version(connection, "thread-1")
        assert version == (3, 5, 3)
        assert destructive_boundary_is_clear(connection, "thread-1", version) is False
        connection.execute(
            "UPDATE active_turns SET status='cancelled' WHERE turn_order=5"
        )
        connection.execute(
            "UPDATE messages SET seq=10 WHERE input_order=3"
        )
        assert destructive_boundary_is_clear(connection, "thread-1", version) is True
    finally:
        connection.close()
