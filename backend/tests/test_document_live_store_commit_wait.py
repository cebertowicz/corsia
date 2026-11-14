"""A commit in flight on the live store never blocks another connection to it."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import types

import pytest

from doceditor import live_store_journal
from doceditor.live_store import LiveStore


@pytest.fixture(autouse=True)
def _fresh_process():
    live_store_journal._stayed.clear()  # each test is a process that has opened no store yet
    yield
    live_store_journal._stayed.clear()


def _hold_commit(path, held: threading.Event, release: threading.Event) -> None:
    """Stand in for a writer whose commit is stuck in a slow fsync."""
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("UPDATE meta SET store_id=store_id")
    held.set()
    release.wait(30)
    holder.execute("COMMIT")
    holder.close()


def test_store_is_wal(tmp_path):
    store = LiveStore(tmp_path / "state.sqlite")
    assert store._read("PRAGMA journal_mode")[0][0] == "wal"


def test_existing_rollback_journal_store_converts_on_open(tmp_path):
    path = tmp_path / "state.sqlite"
    LiveStore(path)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    store = LiveStore(path)
    assert store._read("PRAGMA journal_mode")[0][0] == "wal"


@pytest.mark.parametrize("table", ["live", "mirror", "publish"])  # publish: damage no open-time read touches
def test_damaged_rollback_journal_store_keeps_its_bytes_on_open(tmp_path, table, caplog):
    path = tmp_path / "state.sqlite"
    LiveStore(path)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA journal_mode=DELETE")  # a store as it was written before WAL
        size = db.execute("PRAGMA page_size").fetchone()[0]
        root = db.execute("SELECT rootpage FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
    data = bytearray(path.read_bytes())
    data[(root - 1) * size:root * size] = b"\xa5" * size
    path.write_bytes(bytes(data))
    with caplog.at_level(logging.WARNING, logger="doceditor.live_store_journal"):
        store = LiveStore(path)  # the open itself must not rewrite the damaged bytes
    assert path.read_bytes() == bytes(data)
    if table == "publish":  # quick_check fails yet the store's reads succeed
        assert store._read("SELECT count(*) FROM meta")[0][0] == 1
    assert [r.getMessage().split(": ", 1)[1].split(" (")[0] for r in caplog.records] == ["quick_check failed"]
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.sqlite"]


class _BusyOnCheck:
    """A connection whose integrity check meets SQLITE_BUSY, as if another writer held the store."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, *args):
        if "quick_check" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_busy_integrity_check_keeps_rollback_mode_and_logs_once_per_process(tmp_path, monkeypatch, caplog):
    path = tmp_path / "state.sqlite"
    LiveStore(path)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    connect = LiveStore._connect
    with monkeypatch.context() as patched:
        patched.setattr(LiveStore, "_connect", lambda self: _BusyOnCheck(connect(self)))
        with caplog.at_level(logging.WARNING, logger="doceditor.live_store_journal"):
            store = LiveStore(path)  # no exception escapes the open
        assert store._read("PRAGMA journal_mode")[0][0] == "delete"
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1
    assert str(path) in warnings[0] and "busy, will convert on the first open after a restart" in warnings[0]
    assert LiveStore(path)._read("PRAGMA journal_mode")[0][0] == "delete"  # no second scan in this process
    live_store_journal._stayed.clear()  # a restarted process
    assert LiveStore(path)._read("PRAGMA journal_mode")[0][0] == "wal"


class _ReaderAppearsBeforeSwitch:
    """A connection that meets another connection's read lock just as it switches to WAL."""

    def __init__(self, connection, path, readers):
        self._connection, self._path, self._readers = connection, path, readers

    def execute(self, sql, *args):
        if "journal_mode=WAL" in sql:
            reader = sqlite3.connect(self._path, isolation_level=None)
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM meta").fetchall()  # a shared lock the switch must wait for
            self._readers.append(reader)
        return self._connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_lock_held_during_conversion_does_not_refuse_the_open(tmp_path, monkeypatch, caplog):
    path = tmp_path / "state.sqlite"
    LiveStore(path)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    readers: list[sqlite3.Connection] = []
    connect = LiveStore._connect
    try:
        with monkeypatch.context() as patched:
            patched.setattr(LiveStore, "_connect", lambda self: _ReaderAppearsBeforeSwitch(connect(self), path, readers))
            with caplog.at_level(logging.WARNING, logger="doceditor.live_store_journal"):
                store = LiveStore(path)  # waits out the 1 s busy timeout, then carries on
            assert store._read("PRAGMA journal_mode")[0][0] == "delete"
    finally:
        for reader in readers:
            reader.close()
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "busy, will convert on the first open after a restart" in warnings[0]
    live_store_journal._stayed.clear()  # a restarted process
    assert LiveStore(path)._read("PRAGMA journal_mode")[0][0] == "wal"


class _CountingCheck:
    """Counts integrity checks, and optionally answers the switch to WAL with the mode SQLite stayed in."""

    def __init__(self, connection, checks, stuck_in=None):
        self._connection, self._checks, self._stuck_in = connection, checks, stuck_in

    def execute(self, sql, *args):
        if "quick_check" in sql:
            self._checks.append(sql)
        if self._stuck_in and "journal_mode=WAL" in sql:
            return types.SimpleNamespace(fetchone=lambda: (self._stuck_in,))
        return self._connection.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_failing_store_is_scanned_once_per_process(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    LiveStore(path)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        size = db.execute("PRAGMA page_size").fetchone()[0]
        root = db.execute("SELECT rootpage FROM sqlite_master WHERE name='publish'").fetchone()[0]
    data = bytearray(path.read_bytes())
    data[(root - 1) * size:root * size] = b"\xa5" * size
    path.write_bytes(bytes(data))
    checks: list[str] = []
    connect = LiveStore._connect
    monkeypatch.setattr(LiveStore, "_connect", lambda self: _CountingCheck(connect(self), checks))
    LiveStore(path)
    LiveStore(path)
    assert len(checks) == 1


def test_store_sqlite_cannot_switch_to_wal_says_which_mode_it_is_in(tmp_path, monkeypatch, caplog):
    path = tmp_path / "state.sqlite"
    LiveStore(path)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    checks: list[str] = []
    connect = LiveStore._connect
    monkeypatch.setattr(LiveStore, "_connect", lambda self: _CountingCheck(connect(self), checks, stuck_in="delete"))
    with caplog.at_level(logging.WARNING, logger="doceditor.live_store_journal"):
        store = LiveStore(path)
        LiveStore(path)
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and str(path) in warnings[0] and "still in delete mode" in warnings[0]
    assert store._read("PRAGMA journal_mode")[0][0] == "delete"
    assert len(checks) == 1


@pytest.mark.parametrize("operation", ["read", "connect_for_write_check"])
def test_connection_does_not_wait_for_commit_in_flight(tmp_path, operation):
    store = LiveStore(tmp_path / "state.sqlite")
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    writer = threading.Thread(target=_hold_commit, args=(store.path, held, release))
    writer.start()
    assert held.wait(5)
    outcome: list[object] = []

    def other_connection() -> None:
        started = time.monotonic()
        try:
            store.owed_count() if operation == "read" else store._connect().close()
            outcome.append(time.monotonic() - started)
        except Exception as error:  # the failure under test is any refusal
            outcome.append(error)
        done.set()

    reader = threading.Thread(target=other_connection)
    reader.start()
    finished = done.wait(3)  # the pre-fix store blocks here until the holder commits or the 1 s timeout refuses
    release.set()
    writer.join(10)
    reader.join(10)
    assert finished, "a connection blocked behind a commit in flight"
    assert not isinstance(outcome[0], Exception), outcome[0]
