"""A log reader can tell every ``database is locked`` refusal apart by its SQLite error name."""

from __future__ import annotations

import sqlite3

import pytest

from infra import database
from subagents.codex_app_server_reader import _reader_exit_error


def _connect(path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=0")
    return connection


def _refusal(path, *, snapshot: bool) -> sqlite3.OperationalError:
    """The real error SQLite raises: plain BUSY, or BUSY_SNAPSHOT from a stale read-to-write upgrade."""
    first, second = _connect(path), _connect(path)
    try:
        first.execute("PRAGMA journal_mode=WAL")
        first.execute("CREATE TABLE IF NOT EXISTS t(x)")
        if snapshot:
            first.execute("BEGIN")  # DEFERRED: the read below pins a snapshot
            first.execute("SELECT count(*) FROM t").fetchall()
            second.execute("INSERT INTO t VALUES (1)")  # commits past that snapshot
            write = first
        else:
            first.execute("BEGIN IMMEDIATE")
            second.execute("BEGIN")
            write = second
        with pytest.raises(sqlite3.OperationalError, match="database is locked") as refused:
            write.execute("INSERT INTO t VALUES (2)")
        return refused.value
    finally:
        first.close()
        second.close()


def test_plain_busy_is_named_in_the_lock_log(tmp_path, caplog) -> None:
    error = _refusal(tmp_path / "busy.db", snapshot=False)
    database._log_database_lock("busy_scope", error)
    assert "SQLite database is locked: refused_scope=busy_scope holders=" in caplog.text
    assert "errorname=SQLITE_BUSY" in caplog.text and "errorname=SQLITE_BUSY_SNAPSHOT" not in caplog.text


def test_busy_snapshot_is_named_in_the_lock_log(tmp_path, caplog) -> None:
    error = _refusal(tmp_path / "snapshot.db", snapshot=True)
    database._log_database_lock("snapshot_scope", error)
    assert "errorcode=517 errorname=SQLITE_BUSY_SNAPSHOT" in caplog.text


def test_bridge_error_text_names_a_sqlite_crash_and_leaves_others_alone(tmp_path) -> None:
    class _Session:
        _terminal_error = None

    error = _refusal(tmp_path / "bridge.db", snapshot=True)
    prefix = "CORSIA Codex app-server bridge error: OperationalError: database is locked"
    assert _reader_exit_error(_Session(), error, "") == f"{prefix} [SQLITE_BUSY_SNAPSHOT]"
    assert _reader_exit_error(_Session(), RuntimeError("boom"), "") == (
        "CORSIA Codex app-server bridge error: RuntimeError: boom"
    )
    synthetic = sqlite3.OperationalError("database is locked")  # no SQLite code: no suffix
    assert _reader_exit_error(_Session(), synthetic, "") == prefix
