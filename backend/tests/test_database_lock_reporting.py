from __future__ import annotations

import sqlite3

import pytest

import infra.write_admission as write_admission
from infra import database
from infra.database import write_transaction


def _connection(path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.execute("PRAGMA busy_timeout=25")
    return connection


def test_locked_write_logs_refused_and_holding_scopes(tmp_path, caplog) -> None:
    database_path = tmp_path / "locked.db"
    holder = _connection(database_path)
    refused = _connection(database_path)

    try:
        with write_transaction(holder, scope="holder_scope"):
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                with write_transaction(refused, scope="refused_scope"):
                    pass
        assert "refused_scope" in caplog.text
        assert "holder_scope" in caplog.text
        assert "held=" in caplog.text
    finally:
        holder.close()
        refused.close()


def test_long_write_hold_warns_only_after_threshold(tmp_path, caplog, monkeypatch) -> None:
    class _FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = _FakeClock()
    monkeypatch.setattr(database, "time", clock)
    monkeypatch.setattr(database, "WRITE_TRANSACTION_SLOW_SECONDS", 0.01)
    connection = _connection(tmp_path / "slow.db")

    try:
        with write_transaction(connection, scope="quick_scope"):
            pass
        assert "quick_scope" not in caplog.text

        with write_transaction(connection, scope="slow_scope"):
            clock.now = 0.02
        assert "slow_scope" in caplog.text
        assert "held=" in caplog.text
    finally:
        connection.close()


def test_nested_savepoint_keeps_the_outer_write_holder(tmp_path) -> None:
    connection = _connection(tmp_path / "nested.db")

    try:
        with write_transaction(connection, scope="outer_scope"):
            with write_transaction(connection, scope="nested_scope"):
                held = write_admission.holders_summary()
                assert "scope=outer_scope" in held and "nested_scope" not in held
    finally:
        connection.close()


def test_write_holder_clears_after_success(tmp_path) -> None:
    connection = _connection(tmp_path / "success.db")

    try:
        with write_transaction(connection, scope="success_scope"):
            pass
        assert write_admission.holders_summary() == "none"
    finally:
        connection.close()


def test_write_holder_clears_after_exception(tmp_path) -> None:
    connection = _connection(tmp_path / "exception.db")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            with write_transaction(connection, scope="exception_scope"):
                raise RuntimeError("boom")
        assert write_admission.holders_summary() == "none"
    finally:
        connection.close()


def test_non_sqlite_error_mentioning_a_lock_is_not_reported_as_contention(
    tmp_path, caplog,
) -> None:
    """Text alone must not classify: this codebase raises strings naming a lock."""
    connection = _connection(tmp_path / "false_positive.db")
    bridge_text = "CORSIA Codex app-server bridge error: OperationalError: database is locked"

    try:
        with pytest.raises(RuntimeError, match="database is locked"):
            with write_transaction(connection, scope="bridge_scope"):
                raise RuntimeError(bridge_text)
        assert "SQLite database is locked" not in caplog.text
        assert write_admission.holders_summary() == "none"
    finally:
        connection.close()
