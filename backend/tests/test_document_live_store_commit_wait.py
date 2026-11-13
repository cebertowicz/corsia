"""A commit in flight on the live store never blocks another connection to it."""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from doceditor.live_store import LiveStore


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


@pytest.mark.parametrize("table", ["live", "mirror"])
def test_damaged_rollback_journal_store_keeps_its_bytes_on_open(tmp_path, table):
    path = tmp_path / "state.sqlite"
    LiveStore(path)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA journal_mode=DELETE")  # a store as it was written before WAL
        size = db.execute("PRAGMA page_size").fetchone()[0]
        root = db.execute("SELECT rootpage FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
    data = bytearray(path.read_bytes())
    data[(root - 1) * size:root * size] = b"\xa5" * size
    path.write_bytes(bytes(data))
    LiveStore(path)  # a refusal or not is the store's business; its bytes are not
    assert path.read_bytes() == bytes(data)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["state.sqlite"]


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
