import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from infra import database, write_admission, write_lock_window


@pytest.fixture
def measurement(tmp_path, monkeypatch):
    path = tmp_path / "main.db"
    ticket = write_admission.ticket_for(str(path))
    monkeypatch.setattr(write_admission, "MAIN_TICKET", ticket)
    window = write_lock_window.WriteLockWindow()
    monkeypatch.setattr(write_lock_window, "window", window)
    monkeypatch.setattr(write_admission, "window", window)
    return path, window


def connection(path):
    return sqlite3.connect(str(path), check_same_thread=False)


def test_window_real_transactions_wait_and_reset(measurement, monkeypatch, caplog):
    path, window = measurement
    clock = SimpleNamespace(now=59.0)
    clock.monotonic = lambda: clock.now
    monkeypatch.setattr(database, "time", clock)
    monkeypatch.setattr(write_admission, "time", clock)
    monkeypatch.setattr(write_lock_window, "time", clock)
    window._started = 0.0
    queued = threading.Event()
    entered = threading.Event()
    finish = threading.Event()
    monkeypatch.setattr(write_admission, "_wait_bound", lambda: (queued.set(), 30)[1])

    def waiter():
        with connection(path) as db:
            with database.write_transaction(db, scope="waiter"):
                entered.set()
                assert finish.wait(5)

    with caplog.at_level(logging.WARNING), ThreadPoolExecutor(max_workers=1) as executor:
        with connection(path) as db:
            with database.write_transaction(db, scope="holder"):
                future = executor.submit(waiter)
                assert queued.wait(5)
                clock.now = 59.2
            assert not any("SQLite write lock window:" in r.message for r in caplog.records)
            assert entered.wait(5)
            clock.now = 60.4
            finish.set()
            future.result(timeout=5)
        lines = [r.message for r in caplog.records if "SQLite write lock window:" in r.message]
        assert lines == [
            "SQLite write lock window: window_seconds=60.4 writes=2 held_seconds=1.40 "
            "short_held_seconds=0.20 max_held_seconds=1.20 waited_seconds=0.20 "
            "waited_writes=1 max_waited_seconds=0.20 top_scopes=[waiter 1/1.20s; holder 1/0.20s]"
        ]
        print(lines[0])
        assert window.writes == 0 and window.waited == 0
        clock.now = 120.5
        assert sum("SQLite write lock window:" in r.message for r in caplog.records) == 1
        with connection(path) as db:
            with database.write_transaction(db, scope="next"):
                clock.now = 120.7
        lines = [r.message for r in caplog.records if "SQLite write lock window:" in r.message]
        assert len(lines) == 2
        assert "writes=1 held_seconds=0.20" in lines[1]
        assert "waited_seconds=0.00 waited_writes=0" in lines[1]


def test_other_and_memory_databases_are_excluded(measurement, tmp_path, caplog):
    _, window = measurement
    window._started = 0
    with caplog.at_level(logging.WARNING):
        for path in (tmp_path / "other.db", ":memory:"):
            with connection(path) as db:
                with database.write_transaction(db, scope="other"):
                    db.execute("CREATE TABLE t (id INTEGER)")
    assert window.writes == 0 and window.waited == 0
    assert not any("SQLite write lock window:" in r.message for r in caplog.records)


def test_concurrent_real_holds_do_not_lose_counts(measurement):
    path, window = measurement

    def writes(_):
        with connection(path) as db:
            for _ in range(10):
                with database.write_transaction(db, scope="parallel"):
                    db.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")

    with ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(writes, range(5)))
    assert window.writes == 50
    assert window.scopes["parallel"][0] == 50
