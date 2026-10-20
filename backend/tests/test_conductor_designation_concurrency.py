from __future__ import annotations

from infra.corsia_agent import ensure_corsia_agent, get_corsia_agent, set_corsia_agent
from infra.conductor import (
    ensure_project_conductor, get_project_conductor, get_scope_coordinator,
    set_project_conductor, set_scope_coordinator,
)

import json
import sqlite3
import threading

import pytest


class _BarrierCursor:
    def __init__(self, cursor: sqlite3.Cursor, barrier: threading.Barrier) -> None:
        self._cursor = cursor
        self._barrier = barrier

    def fetchone(self):
        row = self._cursor.fetchone()
        try:
            self._barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return row


class _ReadBarrierConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        barrier: threading.Barrier,
        scope: str,
    ) -> None:
        self._connection = connection
        self._barrier = barrier
        self._scope = scope

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def execute(self, sql: str, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        normalized = " ".join(sql.split()).lower()
        workspace_read = normalized.startswith("select value from settings")
        project_read = normalized.startswith(
            "select conductor_session_id from projects where id"
        )
        if (self._scope == "workspace" and workspace_read) or (
            self._scope != "workspace" and project_read
        ):
            return _BarrierCursor(cursor, self._barrier)
        return cursor

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


class _RecordingConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        contender: sqlite3.Connection,
    ) -> None:
        self._connection = connection
        self._contender = contender
        self.statements: list[str] = []
        self.competing_writer_blocked = False

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    def execute(self, sql: str, parameters=()):
        normalized = " ".join(sql.split()).lower()
        self.statements.append(normalized)
        cursor = self._connection.execute(sql, parameters)
        if normalized == "update settings set value=value where 0":
            try:
                self._contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                self.competing_writer_blocked = "locked" in str(error).lower()
            else:
                self._contender.rollback()
        return cursor

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


def _connection(path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


@pytest.mark.parametrize("scope", ["workspace", "project-a"])
def test_concurrent_designation_invalidates_the_conductor_each_write_replaced(
    tmp_path,
    scope,
):
    from infra.conductor import set_scope_coordinator

    db_path = tmp_path / "designation.db"
    control = _connection(db_path)
    control.executescript(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            conductor_thread_id TEXT,
            updated_at TEXT
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            runtime_context_epoch INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE context_change_journal (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            object_kind TEXT,
            object_id TEXT,
            change_kind TEXT,
            summary TEXT,
            details_json TEXT,
            actor_thread_id TEXT,
            created_at TEXT
        );
        """
    )
    control.executemany(
        "INSERT INTO threads (id,user_id,updated_at) VALUES (?, 'user', 't')",
        [("old",), ("writer-a",), ("writer-b",)],
    )
    control.execute(
        "INSERT INTO projects "
        "(id,name,conductor_thread_id,updated_at) VALUES (?,?,?,?)",
        ("project-a", "Project A", "old" if scope != "workspace" else None, "t"),
    )
    if scope == "workspace":
        control.execute(
            "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
            (
                "corsia_agent_thread_id",
                json.dumps({"session_id": "old"}),
                "t",
            ),
        )
    control.commit()

    read_barrier = threading.Barrier(2)
    start_barrier = threading.Barrier(2)
    results: dict[str, bool] = {}
    failures: list[BaseException] = []

    def designate(thread_id: str) -> None:
        connection = _connection(db_path)
        wrapped = _ReadBarrierConnection(connection, read_barrier, scope)
        try:
            start_barrier.wait(timeout=2)
            results[thread_id] = set_scope_coordinator(scope, thread_id, db=wrapped)
        except BaseException as error:
            failures.append(error)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=designate, args=(thread_id,))
        for thread_id in ("writer-a", "writer-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert results == {"writer-a": True, "writer-b": True}
    if scope == "workspace":
        value = control.execute(
            "SELECT value FROM settings WHERE key='corsia_agent_thread_id'"
        ).fetchone()["value"]
        final_conductor = json.loads(value)["session_id"]
    else:
        final_conductor = control.execute(
            "SELECT conductor_thread_id FROM projects WHERE id='project-a'"
        ).fetchone()["conductor_thread_id"]
    epochs = dict(
        control.execute(
            "SELECT id,runtime_context_epoch FROM threads ORDER BY id"
        ).fetchall()
    )
    if scope == "workspace":
        assert control.execute(
            "SELECT COUNT(*) FROM context_change_journal"
        ).fetchone()[0] == 2
    control.close()

    replaced_conductor = ({"writer-a", "writer-b"} - {final_conductor}).pop()
    assert epochs == {
        "old": 1,
        final_conductor: 1,
        replaced_conductor: 2,
    }


def test_caller_deferred_transaction_acquires_write_lock_before_designation_read(
    tmp_path,
):
    from infra.conductor import set_scope_coordinator

    connection = _connection(tmp_path / "caller-transaction.db")
    connection.executescript(
        """
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            conductor_thread_id TEXT,
            updated_at TEXT
        );
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            runtime_context_epoch INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT
        );
        CREATE TABLE context_change_journal (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            object_kind TEXT,
            object_id TEXT,
            change_kind TEXT,
            summary TEXT,
            details_json TEXT,
            actor_thread_id TEXT,
            created_at TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO threads (id,user_id,updated_at) VALUES (?, 'user', 't')",
        [("old",), ("new",)],
    )
    connection.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)",
        (
            "corsia_agent_thread_id",
            json.dumps({"session_id": "old"}),
            "t",
        ),
    )
    connection.commit()
    connection.execute("BEGIN DEFERRED")
    connection.execute("SELECT id FROM threads WHERE id='old'").fetchone()
    contender = sqlite3.connect(
        tmp_path / "caller-transaction.db",
        timeout=0,
        check_same_thread=False,
    )
    contender.execute("PRAGMA busy_timeout=0")
    wrapped = _RecordingConnection(connection, contender)

    assert set_corsia_agent("new", db=wrapped)

    lock_index = wrapped.statements.index("update settings set value=value where 0")
    read_index = next(
        index
        for index, statement in enumerate(wrapped.statements)
        if statement.startswith("select value from settings")
    )
    assert lock_index < read_index
    assert wrapped.competing_writer_blocked
    assert connection.in_transaction
    assert connection.execute(
        "SELECT COUNT(*) FROM context_change_journal"
    ).fetchone()[0] == 1
    connection.rollback()
    assert connection.execute(
        "SELECT COUNT(*) FROM context_change_journal"
    ).fetchone()[0] == 0
    contender.close()
    connection.close()
