"""All endings of a queued admission contribute to the main lock window."""

import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from infra import database, write_admission, write_lock_window


@pytest.fixture
def setup_window(tmp_path, monkeypatch):
    path = tmp_path / "main.db"
    ticket = write_admission.ticket_for(str(path))
    monkeypatch.setattr(write_admission, "MAIN_TICKET", ticket)
    window = write_lock_window.WriteLockWindow()
    monkeypatch.setattr(write_admission, "window", window)
    monkeypatch.setattr(write_lock_window, "window", window)
    return path, ticket, window


def test_timeout_is_recorded_and_other_ticket_is_excluded(setup_window, tmp_path, monkeypatch, caplog):
    path, ticket, window = setup_window
    monkeypatch.setattr(write_admission, "WORKER_WAIT_SECONDS", 0.06)
    for target in (path, tmp_path / "other.db"):
        held, release = threading.Event(), threading.Event()

        def holder():
            with sqlite3.connect(target) as db:
                with database.write_transaction(db, scope="holder"):
                    held.set()
                    assert release.wait(5)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(holder)
            assert held.wait(5)
            try:
                with sqlite3.connect(target) as db, pytest.raises(sqlite3.OperationalError):
                    with database.write_transaction(db, scope="timed_out"):
                        pass
            finally:
                release.set()
                future.result(timeout=5)
        if target == path:
            measured = window.waited
            assert measured >= 0.05
            assert window.waited_writes == 1
            assert window.max_waited == measured
        else:
            assert window.waited == measured
            assert window.waited_writes == 1
            assert window.max_waited == measured
    assert ticket is not None
    with caplog.at_level(logging.WARNING):
        window._started -= 61
        window.hold("sample", 0.0, True)
    assert f"waited_seconds={measured:.2f} waited_writes=1 max_waited_seconds={measured:.2f}" in caplog.text


def test_withdrawn_wait_is_recorded(setup_window, monkeypatch, caplog):
    path, ticket, window = setup_window
    held, release = threading.Event(), threading.Event()
    original_admission = write_admission.Admission

    def holder():
        with sqlite3.connect(path) as db:
            with database.write_transaction(db, scope="holder"):
                held.set()
                assert release.wait(5)

    class InterruptedEvent:
        def wait(self, timeout):
            raise KeyboardInterrupt("interrupted")

    def admission(*args, **kwargs):
        item = original_admission(*args, **kwargs)
        item.granted = InterruptedEvent()
        return item

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(holder)
        assert held.wait(5)
        monkeypatch.setattr(write_admission, "Admission", admission)
        original_clock = write_admission.time
        ticks = iter([10.0, 10.1])
        monkeypatch.setattr(write_admission, "time", type("Clock", (), {"monotonic": staticmethod(lambda: next(ticks))})())
        try:
            with sqlite3.connect(path) as db, pytest.raises(KeyboardInterrupt):
                with database.write_transaction(db, scope="withdrawn"):
                    pass
            assert window.waited == pytest.approx(0.1)
            assert window.waited_writes == 1
            assert window.max_waited == pytest.approx(0.1)
            assert not ticket._waiters
            with caplog.at_level(logging.WARNING):
                window._started -= 61
                window.hold("sample", 0.0, True)
            assert "waited_seconds=0.10 waited_writes=1 max_waited_seconds=0.10" in caplog.text
        finally:
            monkeypatch.setattr(write_admission, "time", original_clock)
            release.set()
            future.result(timeout=5)
