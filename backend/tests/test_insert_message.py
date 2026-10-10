
from __future__ import annotations

import pathlib
import sqlite3
import sys
import os
import threading
import uuid

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


UNBOUND = {"turn_unbound_reason": "legacy_import"}


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, user_id TEXT, title TEXT,
            model TEXT, reasoning_effort TEXT,
            spawned_by TEXT, project_id TEXT,
            created_at TEXT, updated_at TEXT,
            parent_thread_id TEXT,
            next_message_seq INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT NOT NULL, metadata TEXT, created_at TEXT NOT NULL,
            seq INTEGER,
            FOREIGN KEY (thread_id) REFERENCES threads(id)
        );
        CREATE UNIQUE INDEX idx_messages_session_seq ON messages(thread_id, seq);
        INSERT INTO threads (id, user_id, created_at, updated_at)
        VALUES ('s1', 'u1', 't', 't');
    """)
    conn.commit()
    return conn


class _NoClose:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def close(self):
        pass  # keep the connection open so the test can inspect rows



def test_insert_message_no_db_commits(monkeypatch):
    conn = _make_db()
    wrapper = _NoClose(conn)

    from infra import database, queries
    monkeypatch.setattr(database, "get_db", lambda: wrapper)

    mid = queries.insert_message("s1", "user", "hello")

    row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    assert row is not None
    assert row["content"] == "hello"
    assert row["role"] == "user"
    assert row["seq"] == 1
    assert conn.execute(
        "SELECT next_message_seq FROM threads WHERE id='s1'"
    ).fetchone()[0] == 2


def test_insert_message_with_caller_transaction_defers_commit(monkeypatch):
    conn = _make_db()

    from infra import database, queries
    from infra.database import write_transaction

    monkeypatch.setattr(database, "get_db", lambda: (_ for _ in ()).throw(
        AssertionError("get_db called despite db= being passed")))

    with write_transaction(conn, scope="test_message_visibility") as db:
        mid = queries.insert_message("s1", "assistant", "deferred", UNBOUND, db=db)
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
        assert row is not None, "row should be readable within the caller transaction"

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db_path = pathlib.Path(td) / "test.db"
        _make_file_db(db_path)
        c1 = sqlite3.connect(str(db_path))
        c1.row_factory = sqlite3.Row

        with write_transaction(c1, scope="test_message_visibility") as db:
            mid2 = queries.insert_message("s1", "assistant", "deferred", UNBOUND, db=db)

            c2 = sqlite3.connect(str(db_path))
            c2.row_factory = sqlite3.Row
            row_before = c2.execute(
                "SELECT * FROM messages WHERE id = ?", (mid2,),
            ).fetchone()
            assert row_before is None, "row must not be committed until caller commits"
            assert c2.execute(
                "SELECT next_message_seq FROM threads WHERE id='s1'"
            ).fetchone()[0] == 1

        c2_after = sqlite3.connect(str(db_path))
        c2_after.row_factory = sqlite3.Row
        row_after = c2_after.execute(
            "SELECT * FROM messages WHERE id = ?", (mid2,),
        ).fetchone()
        assert row_after is not None
        assert row_after["content"] == "deferred"
        assert row_after["seq"] == 1
        assert c2_after.execute(
            "SELECT next_message_seq FROM threads WHERE id='s1'"
        ).fetchone()[0] == 2

        c1.close()
        c2.close()
        c2_after.close()


def test_insert_message_metadata_serialised():
    conn = _make_db()
    wrapper = _NoClose(conn)

    from infra import queries
    import json

    mid = queries.insert_message("s1", "system", "meta test",
                                 metadata={"type": "x", "n": 42}, db=conn)
    conn.commit()
    row = conn.execute("SELECT metadata FROM messages WHERE id = ?", (mid,)).fetchone()
    assert json.loads(row["metadata"]) == {"type": "x", "n": 42}

    mid2 = queries.insert_message("s1", "system", "no meta", metadata=None, db=conn)
    conn.commit()
    row2 = conn.execute("SELECT metadata FROM messages WHERE id = ?", (mid2,)).fetchone()
    assert row2["metadata"] is None

    conn.close()


def test_insert_message_default_id_is_full_uuid4():
    conn = _make_db()

    from infra import queries

    message_id = queries.insert_message("s1", "user", "uuid", db=conn)

    parsed = uuid.UUID(message_id)
    assert parsed.version == 4
    assert str(parsed) == message_id


def test_caller_savepoint_preserves_earlier_work_and_does_not_burn_sequence():
    conn = _make_db()
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,created_at,seq) "
        "VALUES ('duplicate','s1','user','existing','t',1)"
    )
    conn.execute("UPDATE threads SET next_message_seq=2 WHERE id='s1'")
    conn.commit()
    conn.execute("UPDATE threads SET title='earlier caller work' WHERE id='s1'")

    from infra import queries

    with pytest.raises(sqlite3.IntegrityError):
        queries.insert_message(
            "s1", "assistant", "duplicate", UNBOUND, msg_id="duplicate", db=conn
        )

    assert conn.execute(
        "SELECT next_message_seq FROM threads WHERE id='s1'"
    ).fetchone()[0] == 2
    inserted = queries.insert_message(
        "s1", "assistant", "successor", UNBOUND, msg_id="successor", db=conn
    )
    assert inserted == "successor"
    assert conn.execute(
        "SELECT seq FROM messages WHERE id='successor'"
    ).fetchone()[0] == 2
    conn.commit()
    assert conn.execute(
        "SELECT title FROM threads WHERE id='s1'"
    ).fetchone()[0] == "earlier caller work"


def test_owned_failure_rolls_back_reservation_and_leaves_connection_clean(monkeypatch):
    conn = _make_db()
    conn.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,created_at,seq) "
        "VALUES ('duplicate','s1','user','existing','t',1)"
    )
    conn.execute("UPDATE threads SET next_message_seq=2 WHERE id='s1'")
    conn.commit()

    from infra import database, queries

    monkeypatch.setattr(database, "get_db", lambda: _NoClose(conn))
    with pytest.raises(sqlite3.IntegrityError):
        queries.insert_message("s1", "assistant", "duplicate", UNBOUND, msg_id="duplicate")

    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT next_message_seq FROM threads WHERE id='s1'"
    ).fetchone()[0] == 2
    queries.insert_message("s1", "assistant", "successor", UNBOUND, msg_id="successor")
    row = conn.execute(
        "SELECT seq FROM messages WHERE id='successor'"
    ).fetchone()
    assert row[0] == 2


def test_owned_insert_rejects_dirty_pooled_connection_without_mutating_it(monkeypatch):
    conn = _make_db()
    conn.execute("UPDATE threads SET title='owned elsewhere' WHERE id='s1'")

    from infra import database, queries
    from infra.database import TransactionOwnershipError

    monkeypatch.setattr(database, "get_db", lambda: _NoClose(conn))
    with pytest.raises(TransactionOwnershipError, match="claimed the outermost"):
        queries.insert_message("s1", "user", "must not erase caller work")

    assert conn.in_transaction is True
    assert conn.execute(
        "SELECT title FROM threads WHERE id='s1'"
    ).fetchone()[0] == "owned elsewhere"
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    conn.rollback()


def test_savepoint_creation_failure_preserves_original_error():
    conn = _make_db()

    class _FailSavepoint:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, parameters=()):
            if sql.startswith("SAVEPOINT "):
                raise sqlite3.OperationalError("savepoint unavailable")
            return conn.execute(sql, parameters)

    from infra import queries
    from infra.database import write_transaction

    with write_transaction(conn, scope="test_savepoint_failure") as db:
        with pytest.raises(sqlite3.OperationalError, match="savepoint unavailable"):
            queries.insert_message(
                "s1", "user", "cannot reserve", db=_FailSavepoint()
            )
        assert db.in_transaction is True

    with write_transaction(conn, scope="test_savepoint_successor") as db:
        successor = queries.insert_message(
            "s1", "assistant", "usable again", UNBOUND, msg_id="usable-again", db=db,
        )
        assert successor == "usable-again"


def test_caller_transaction_recovers_after_inner_failure(monkeypatch):
    conn = _make_db()

    from infra import queries
    from infra.database import write_transaction

    def fail_admission(_db, _turn_id):
        raise sqlite3.OperationalError("forced admission failure")

    monkeypatch.setattr(queries, "assert_turn_accepts_tool_calls", fail_admission)
    with write_transaction(conn, scope="test_failure_recovery") as db:
        with pytest.raises(sqlite3.OperationalError, match="forced admission failure"):
            queries.insert_message(
                "s1",
                "assistant",
                "tool batch",
                metadata={"turn_id": "turn-1", "llm_tool_calls": [{"id": "call-1"}]},
                db=db,
            )
        assert db.in_transaction is True

        monkeypatch.setattr(
            queries, "assert_turn_accepts_tool_calls", lambda *_args: None,
        )
        message_id = queries.insert_message(
            "s1",
            "assistant",
            "recovered",
            UNBOUND,
            msg_id="recovered",
            db=db,
        )

    assert conn.execute(
        "SELECT content FROM messages WHERE id=?", (message_id,)
    ).fetchone()[0] == "recovered"


def test_missing_session_failure_preserves_earlier_caller_work():
    conn = _make_db()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("UPDATE threads SET title='keep me' WHERE id='s1'")

    from infra import queries

    with pytest.raises(LookupError, match="missing"):
        queries.insert_message(
            "missing", "user", "no target", msg_id="missing-message", db=conn
        )

    conn.commit()
    assert conn.execute(
        "SELECT title FROM threads WHERE id='s1'"
    ).fetchone()[0] == "keep me"
    assert conn.execute(
        "SELECT 1 FROM messages WHERE id='missing-message'"
    ).fetchone() is None


def _make_file_db(path: pathlib.Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT,
            created_at TEXT,
            updated_at TEXT,
            next_message_seq INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL REFERENCES threads(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT,
            created_at TEXT NOT NULL,
            seq INTEGER
        );
        CREATE UNIQUE INDEX idx_messages_session_seq ON messages(thread_id,seq);
        INSERT INTO threads (id,user_id,created_at,updated_at)
        VALUES ('s1','u','t','t');
        """
    )
    conn.commit()
    conn.close()


def test_two_connections_allocate_distinct_consecutive_sequences(tmp_path):
    db_path = tmp_path / "concurrent.db"
    _make_file_db(db_path)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, int]] = []
    errors: list[BaseException] = []

    from infra import queries

    def _write(message_id: str) -> None:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            barrier.wait()
            queries.insert_message(
                "s1", "user", message_id, msg_id=message_id, db=conn
            )
            seq = conn.execute(
                "SELECT seq FROM messages WHERE id=?", (message_id,)
            ).fetchone()[0]
            conn.commit()
            outcomes.append((message_id, seq))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            conn.close()

    threads = [
        threading.Thread(target=_write, args=(message_id,))
        for message_id in ("m1", "m2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert sorted(seq for _, seq in outcomes) == [1, 2]
    conn = sqlite3.connect(str(db_path))
    assert conn.execute(
        "SELECT next_message_seq FROM threads WHERE id='s1'"
    ).fetchone()[0] == 3
    conn.close()


def test_clean_caller_acquires_write_lock_before_tool_admission_read(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "snapshot-upgrade.db"
    _make_file_db(db_path)

    c1 = sqlite3.connect(str(db_path), timeout=0)
    c2 = sqlite3.connect(str(db_path), timeout=0)
    try:
        c1.execute("PRAGMA journal_mode=WAL")
        c1.commit()
        c2.execute("CREATE TABLE race_marker (id INTEGER PRIMARY KEY)")
        c2.commit()

        from infra import queries

        writer_errors = []

        def admit_tool_call(db, _turn_id):
            # This read is the snapshot that used to be upgraded by the
            # subsequent message reservation UPDATE.
            db.execute("SELECT 1").fetchone()
            try:
                c2.execute("INSERT INTO race_marker DEFAULT VALUES")
                c2.commit()
            except sqlite3.OperationalError as exc:
                writer_errors.append(str(exc))
                c2.rollback()

        monkeypatch.setattr(queries, "assert_turn_accepts_tool_calls", admit_tool_call)
        message_id = queries.insert_message(
            "s1",
            "assistant",
            "tool batch",
            metadata={"turn_id": "turn-1", "llm_tool_calls": [{"id": "call-1"}]},
            msg_id="snapshot-recovered",
            db=c1,
        )
        c1.commit()

        assert writer_errors == ["database is locked"]
        assert c1.execute(
            "SELECT content FROM messages WHERE id=?", (message_id,)
        ).fetchone()[0] == "tool batch"
    finally:
        c1.close()
        c2.close()


def test_rollback_reuses_never_durable_sequence_but_delete_does_not(tmp_path):
    db_path = tmp_path / "reuse.db"
    _make_file_db(db_path)
    from infra import queries
    from infra.database import write_transaction

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    with pytest.raises(RuntimeError, match="rollback caller boundary"):
        with write_transaction(conn, scope="test_rollback_message") as db:
            queries.insert_message(
                "s1", "user", "rolled back", msg_id="rolled-back", db=db,
            )
            assert conn.execute(
                "SELECT seq FROM messages WHERE id='rolled-back'"
            ).fetchone()[0] == 1
            raise RuntimeError("rollback caller boundary")

    with write_transaction(conn, scope="test_committed_message") as db:
        queries.insert_message("s1", "user", "committed", msg_id="committed", db=db)
        assert conn.execute(
            "SELECT seq FROM messages WHERE id='committed'"
        ).fetchone()[0] == 1
    conn.execute("DELETE FROM messages WHERE id='committed'")
    conn.commit()

    with write_transaction(conn, scope="test_successor_message") as db:
        queries.insert_message("s1", "user", "successor", msg_id="successor", db=db)
        assert conn.execute(
            "SELECT seq FROM messages WHERE id='successor'"
        ).fetchone()[0] == 2
    conn.close()


def test_fifo_accept_keeps_pending_input_unsequenced_and_rolls_back_conflict(
    isolated_db,
):
    """The send path has one queued canonical row, never a copy."""
    from infra.database import get_db
    from infra.input_admission import accept_thread_input

    db = get_db()
    try:
        db.executemany(
            "INSERT INTO threads "
            "(id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
            [
                ("inbox-s1", "u", "claude-code/sonnet", "t", "t"),
                ("inbox-s2", "u", "claude-code/sonnet", "t", "t"),
            ],
        )
        db.commit()
    finally:
        db.close()

    receipt = accept_thread_input(
        "inbox-s1",
        role="user",
        content="durable pending thought",
        intent="apply_now",
        message_id="inbox-input-1",
        created_at="t1",
    )
    assert receipt.message_id == "inbox-input-1"

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT delivery_state,seq FROM messages WHERE id=?",
            ("inbox-input-1",),
        ).fetchone()) == ("queued", None)
        assert db.execute(
            "SELECT next_message_seq FROM threads WHERE id='inbox-s1'"
        ).fetchone()[0] == 1
    finally:
        db.close()

    with pytest.raises(ValueError, match="different payload"):
        accept_thread_input(
            "inbox-s2",
            role="user",
            content="conflicting duplicate",
            intent="apply_now",
            message_id="inbox-input-1",
            created_at="t2",
        )

    db = get_db()
    try:
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='inbox-s2'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT next_message_seq FROM threads WHERE id='inbox-s2'"
        ).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize("role", ["assistant", "tool"])
def test_insert_message_refuses_an_assistant_or_tool_row_bound_to_no_turn(role):
    conn = _make_db()
    from infra import queries

    with pytest.raises(ValueError, match="no Turn provenance"):
        queries.insert_message("s1", role, "orphan", {"for_user": True}, db=conn)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_insert_message_accepts_bound_rows_and_unbound_user_rows():
    conn = _make_db()
    from infra import queries

    queries.insert_message("s1", "assistant", "a", {"turn_id": "turn-1"}, db=conn)
    queries.insert_message("s1", "tool", "t", UNBOUND, db=conn)
    queries.insert_message("s1", "user", "u", None, db=conn)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3


@pytest.mark.parametrize(
    ("metadata", "match"),
    [
        ({"turn_unbound_reason": "made_up"}, "invalid legacy provenance"),
        ({"turn_id": "turn-1", "turn_unbound_reason": "legacy_import"}, "conflicting Turn provenance"),
        ({"turn_id": 42}, "invalid Turn provenance"),
    ],
)
def test_insert_message_applies_the_projections_own_provenance_rule(metadata, match):
    conn = _make_db()
    from infra import queries

    with pytest.raises(ValueError, match=match):
        queries.insert_message("s1", "assistant", "bad", metadata, db=conn)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("role", ["assistant", "tool", "user"])
def test_insert_message_refuses_the_reader_only_unusable_provenance_label(role):
    conn = _make_db()
    from infra import queries
    from infra.turn_provenance import UNUSABLE_TURN_PROVENANCE_REASON

    with pytest.raises(ValueError, match="invalid legacy provenance"):
        queries.insert_message(
            "s1",
            role,
            "bad",
            {"turn_unbound_reason": UNUSABLE_TURN_PROVENANCE_REASON},
            db=conn,
        )
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
