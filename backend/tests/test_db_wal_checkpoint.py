"""WAL maintenance must not hold admission or delay committed effects."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from infra import database, write_effects
from infra.db_connection import PooledConnection, checkpoint_and_close


def _connection(tmp_path, name="main.db"):
    conn = database._make_connection(str(tmp_path / name))
    conn.execute("CREATE TABLE IF NOT EXISTS payload(id INTEGER PRIMARY KEY, value BLOB)")
    conn.commit()
    return conn


def _large_write(conn):
    with database.write_transaction(conn, scope="large_write"):
        conn.execute("INSERT INTO payload(value) VALUES(?)", (b"x" * 5_000_000,))


def test_pooled_connections_defer_autocheckpoint_and_limit_reused_wal(tmp_path):
    conn = _connection(tmp_path)
    try:
        assert conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 32 + 1000 * (page_size + 24)
    finally:
        conn.close()


def test_checkpoint_releases_admission_and_delivers_effects_first(tmp_path, monkeypatch):
    checkpoint_entered = threading.Event()
    competitor_done = threading.Event()
    effects = []
    errors = []

    class ObservedConnection(PooledConnection):
        def execute(self, sql, parameters=(), /):
            if sql == "PRAGMA wal_checkpoint(PASSIVE)" and not checkpoint_entered.is_set():
                assert effects == ["committed"]
                assert not self.in_transaction
                checkpoint_entered.set()
                assert competitor_done.wait(3), "checkpoint retained writer admission"
            return super().execute(sql, parameters)

    monkeypatch.setattr(database, "PooledConnection", ObservedConnection)
    conn = _connection(tmp_path)

    def compete():
        peer = None
        try:
            assert checkpoint_entered.wait(3), "no explicit post-commit checkpoint ran"
            peer = database._make_connection(str(tmp_path / "main.db"))
            with database.write_transaction(peer, scope="competing_write"):
                peer.execute("INSERT INTO payload(value) VALUES('peer')")
            competitor_done.set()
        except BaseException as error:
            errors.append(error)
        finally:
            if peer is not None:
                peer.close()

    worker = threading.Thread(target=compete)
    worker.start()
    try:
        with database.write_transaction(conn, scope="large_write"):
            conn.execute("INSERT INTO payload(value) VALUES(?)", (b"x" * 5_000_000,))
            write_effects.after_commit(conn, lambda: effects.append("committed"))
        worker.join(5)
        assert not worker.is_alive()
        assert not errors
        assert conn.execute("SELECT count(*) FROM payload").fetchone()[0] == 2
    finally:
        competitor_done.set()
        worker.join(5)
        conn.close()


def test_completed_checkpoint_does_not_make_every_small_write_checkpoint(tmp_path):
    conn = _connection(tmp_path)
    seen = []
    conn.set_trace_callback(lambda sql: seen.append(sql) if sql.startswith("PRAGMA wal_checkpoint") else None)
    try:
        _large_write(conn)
        assert seen == ["PRAGMA wal_checkpoint(PASSIVE)"]
        wal = Path(str(tmp_path / "main.db") + "-wal")
        assert wal.stat().st_size > conn.execute("PRAGMA journal_size_limit").fetchone()[0]
        with database.write_transaction(conn, scope="small_write"):
            conn.execute("INSERT INTO payload(value) VALUES('small')")
        assert seen == ["PRAGMA wal_checkpoint(PASSIVE)"]
        assert wal.stat().st_size <= conn.execute("PRAGMA journal_size_limit").fetchone()[0]
    finally:
        conn.close()


def test_a_relative_path_still_checkpoints_after_the_working_directory_moves(tmp_path, monkeypatch):
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path)
    conn = database._make_connection("main.db")
    conn.execute("CREATE TABLE payload(id INTEGER PRIMARY KEY, value BLOB)")
    conn.commit()
    monkeypatch.chdir(tmp_path / "elsewhere")
    seen = []
    conn.set_trace_callback(lambda sql: seen.append(sql) if sql.startswith("PRAGMA wal_checkpoint") else None)
    try:
        _large_write(conn)
        assert seen == ["PRAGMA wal_checkpoint(PASSIVE)"]
    finally:
        conn.close()


def test_pinned_reader_keeps_its_snapshot_and_shutdown_still_truncates(tmp_path):
    conn = _connection(tmp_path)
    reader = sqlite3.connect(tmp_path / "main.db")
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM payload").fetchone()[0] == 0
        _large_write(conn)
        assert reader.execute("SELECT count(*) FROM payload").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM payload").fetchone()[0] == 1
        reader.rollback()
        checkpoint_and_close([conn])
        wal = Path(str(tmp_path / "main.db") + "-wal")
        assert not wal.exists() or wal.stat().st_size == 0
    finally:
        reader.close()
        conn.close()


def test_rollback_and_nested_savepoint_do_not_checkpoint(tmp_path):
    conn = _connection(tmp_path)
    seen = []
    conn.set_trace_callback(lambda sql: seen.append(sql) if sql.startswith("PRAGMA wal_checkpoint") else None)
    try:
        with pytest.raises(ValueError, match="rollback"):
            with database.write_transaction(conn, scope="outer_write"):
                with database.write_transaction(conn, scope="nested_write"):
                    conn.execute("INSERT INTO payload(value) VALUES(?)", (b"x" * 5_000_000,))
                assert seen == []
                raise ValueError("rollback")
        assert seen == []
        assert conn.execute("SELECT count(*) FROM payload").fetchone()[0] == 0
    finally:
        conn.close()


def test_checkpoint_failure_cannot_fail_a_committed_write(tmp_path, monkeypatch, caplog):
    class FailedCheckpoint(PooledConnection):
        def execute(self, sql, parameters=(), /):
            if sql == "PRAGMA wal_checkpoint(PASSIVE)":
                raise sqlite3.OperationalError("synthetic checkpoint failure")
            return super().execute(sql, parameters)

    monkeypatch.setattr(database, "PooledConnection", FailedCheckpoint)
    conn = _connection(tmp_path)
    try:
        _large_write(conn)
        assert conn.execute("SELECT count(*) FROM payload").fetchone()[0] == 1
        assert "post-commit checkpoint failed" in caplog.text
    finally:
        conn.close()


def test_startup_checkpoint_retains_file_activity_progress(tmp_path, monkeypatch):
    from infra import startup_progress

    observed = []

    class StartupCheckpoint(PooledConnection):
        def execute(self, sql, parameters=(), /):
            if sql == "PRAGMA wal_checkpoint(PASSIVE)":
                observed.append(any(t.name == "startup-database-writes" for t in threading.enumerate()))
            return super().execute(sql, parameters)

    monkeypatch.setattr(database, "PooledConnection", StartupCheckpoint)
    conn = _connection(tmp_path)
    startup_progress.begin(tmp_path / "runtime")
    try:
        _large_write(conn)
        assert observed == [True]
    finally:
        startup_progress.finish()
        conn.close()


@pytest.mark.asyncio
async def test_post_release_checkpoint_stays_on_the_callers_db_worker(tmp_path, monkeypatch):
    import asyncio

    from infra.db_async import run_in_db
    from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV

    observed = []

    class WorkerCheckpoint(PooledConnection):
        def execute(self, sql, parameters=(), /):
            if sql == "PRAGMA wal_checkpoint(PASSIVE)":
                with pytest.raises(RuntimeError, match="no running event loop"):
                    asyncio.get_running_loop()
                observed.append(threading.current_thread().name)
            return super().execute(sql, parameters)

    monkeypatch.setattr(database, "PooledConnection", WorkerCheckpoint)
    conn = _connection(tmp_path)
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    try:
        await run_in_db(_large_write, conn)
        assert len(observed) == 1
        assert observed[0].startswith("db_")
    finally:
        conn.close()
