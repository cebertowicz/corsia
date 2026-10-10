"""Real SQLite connections follow their worker lifetime, including thread churn."""

import os
import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from infra import database
from infra.db_connection import PooledConnection


@pytest.mark.parametrize("fail", [False, True])
def test_retired_threads_release_connections(isolated_db, fail):
    keeper = database.get_db()
    baseline = len(database._all_connections)
    opened = queue.Queue()

    def work():
        connection = database.get_db()
        opened.put(connection._conn)
        try:
            connection.execute("SELECT 1").fetchone()
            if fail:
                raise ValueError("worker failure")
        except ValueError:
            pass

    # Retain the Thread objects and raw handles: neither may retain a live
    # connection once the owning thread has finished. No forced GC or sweep.
    threads = [threading.Thread(target=work) for _ in range(24)]
    for thread in threads:
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert opened.qsize() == len(threads)
    assert len(database._all_connections) == baseline
    while not opened.empty():
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened.get_nowait().execute("SELECT 1")
    assert keeper.execute("SELECT 1").fetchone()[0] == 1


def test_active_pool_worker_reuses_connection_until_exit(isolated_db):
    baseline = len(database._all_connections)

    def borrow():
        connection = database.get_db()
        connection.execute("CREATE TABLE IF NOT EXISTS lifetime_probe (value TEXT)")
        connection.execute("INSERT INTO lifetime_probe VALUES ('uncommitted')")
        connection.close()
        assert not connection.in_transaction
        return connection._conn

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(borrow).result(timeout=5)
        second = pool.submit(borrow).result(timeout=5)
        assert first is second
        assert len(database._all_connections) == baseline + 1
        assert first.execute("SELECT count(*) FROM lifetime_probe").fetchone()[0] == 0

    assert len(database._all_connections) == baseline
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        first.execute("SELECT 1")


def test_explicit_close_releases_slot_and_allows_reopen(isolated_db):
    first = database.get_db()._conn
    database.close_thread_db()
    database.close_thread_db()
    assert first not in database._all_connections
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        first.execute("SELECT 1")
    second = database.get_db()._conn
    assert second is not first
    assert second.execute("SELECT 1").fetchone()[0] == 1


def test_failed_configuration_closes_partial_connection(isolated_db, monkeypatch):
    baseline = len(database._all_connections)
    real_connect = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_authorizer(lambda *_args: sqlite3.SQLITE_DENY)
        opened.append(connection)
        return connection

    monkeypatch.setattr(database.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        database._make_connection(str(database.DB_PATH))
    assert len(opened) == 1
    assert len(database._all_connections) == baseline
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


@pytest.mark.parametrize("first_closer", ["thread_exit", "shutdown"])
def test_thread_exit_and_shutdown_close_each_connection_once(isolated_db, monkeypatch, first_closer):
    database.close_thread_db()
    ready = threading.Event()
    exit_worker = threading.Event()
    closing = threading.Event()
    finish_close = threading.Event()
    handles = queue.Queue()
    closes = queue.Queue()
    real_connect = sqlite3.connect

    class Connection(PooledConnection):
        def close(self):
            closes.put(self)
            closing.set()
            assert finish_close.wait(timeout=5)
            super().close()

    def connect(*args, **kwargs):
        return real_connect(*args, **{**kwargs, "factory": Connection})

    monkeypatch.setattr(database.sqlite3, "connect", connect)

    def work():
        db = database.get_db()
        handles.put(db._conn)
        assert db.execute("SELECT 1").fetchone()[0] == 1
        ready.set()  # SQL is finished before either close path starts.
        assert exit_worker.wait(timeout=5)

    worker = threading.Thread(target=work)
    shutdown = threading.Thread(target=database.shutdown_pool)
    worker.start()
    try:
        assert ready.wait(timeout=5)
        if first_closer == "thread_exit":
            exit_worker.set()
            assert closing.wait(timeout=5)
            shutdown.start()
            shutdown.join(timeout=5)
            assert not shutdown.is_alive()
        else:
            shutdown.start()
            assert closing.wait(timeout=5)
            exit_worker.set()
            worker.join(timeout=5)
            assert not worker.is_alive()
    finally:
        exit_worker.set()
        finish_close.set()
        worker.join(timeout=5)
        if shutdown.ident is not None:
            shutdown.join(timeout=5)

    assert not worker.is_alive() and not shutdown.is_alive()
    assert closes.qsize() == 1
    assert not database._all_connections
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        handles.get_nowait().execute("SELECT 1")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork-only regression")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_forked_child_never_releases_parent_connections(isolated_db, monkeypatch):
    """A fork clears other threads' locals in the child; that must not close the parent's handles.

    uvloop's subprocess spawn runs PyOS_AfterFork_Child before exec. Releasing
    there closed parent SQLite handles in the child, which deadlocked on the
    WAL mutex while still holding the server's listen socket.
    """
    read_fd, write_fd = os.pipe()
    real_release = database._release_connection

    def recording_release(conn):
        os.write(write_fd, f"{os.getpid()}\n".encode())
        if os.getpid() == parent_pid:
            real_release(conn)

    monkeypatch.setattr(database, "_release_connection", recording_release)
    parent_pid = os.getpid()
    ready = threading.Event()
    exit_worker = threading.Event()

    def work():
        database.get_db().execute("SELECT 1").fetchone()
        ready.set()
        assert exit_worker.wait(timeout=5)

    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert ready.wait(timeout=5)
        child = os.fork()
        if child == 0:
            os._exit(0)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        exit_worker.set()
        worker.join(timeout=5)
    os.close(write_fd)
    with os.fdopen(read_fd) as releases:
        assert releases.read().split() == [str(parent_pid)]


def _file_of(connection) -> str:
    return connection.execute("PRAGMA database_list").fetchone()["file"]


def test_worker_connection_follows_a_changed_database_path(isolated_db, monkeypatch, tmp_path):
    before = database.DB_PATH
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(lambda: database.get_db()._conn).result(timeout=5)
        assert _file_of(first) == str(before)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "next.db")
        second = pool.submit(lambda: database.get_db()._conn).result(timeout=5)
        assert second is not first
        assert _file_of(second) == str(tmp_path / "next.db")
        assert first not in database._all_connections
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            first.execute("SELECT 1")


def test_open_transaction_keeps_its_connection_across_a_path_change(isolated_db, monkeypatch, tmp_path):
    connection = database.get_db()
    connection.execute("CREATE TABLE IF NOT EXISTS path_probe (value TEXT)")
    connection.execute("INSERT INTO path_probe VALUES ('pending')")
    assert connection.in_transaction
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "next.db")
    assert database.get_db()._conn is connection._conn
    connection.commit()
    assert database.get_db()._conn is not connection._conn


def test_connection_is_recorded_under_the_path_it_opened(isolated_db, monkeypatch, tmp_path):
    """A path change while the connection opens cannot label it with the new path."""
    opened_on = database.DB_PATH
    real_make_connection = database._make_connection

    def open_then_switch(path):
        connection = real_make_connection(path)
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "next.db")
        return connection

    monkeypatch.setattr(database, "_make_connection", open_then_switch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(lambda: database.get_db()._conn).result(timeout=5)
        assert _file_of(first) == str(opened_on)
        monkeypatch.setattr(database, "_make_connection", real_make_connection)
        second = pool.submit(lambda: database.get_db()._conn).result(timeout=5)
        assert _file_of(second) == str(tmp_path / "next.db")
