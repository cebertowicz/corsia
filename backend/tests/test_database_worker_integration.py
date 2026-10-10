"""Exercise SQLite ownership through real FastAPI/AnyIO worker retirement."""

import asyncio
import queue
import sqlite3
import threading

import anyio
import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra import database


def _database_fds(path):
    names = {str(path), f"{path}-wal", f"{path}-shm"}
    return len([entry for entry in psutil.Process().open_files() if entry.path in names])


@pytest.mark.parametrize("fail", [False, True])
def test_http_worker_churn_does_not_accumulate_database_handles(isolated_db, fail):
    connection = database.get_db()
    connection.execute("CREATE TABLE http_lifetime_probe (value TEXT)")
    connection.commit()
    # With another connection alive, SQLite's Unix VFS may defer closing a
    # database descriptor to preserve process-wide locks. Start with no keeper
    # so retirement must return the actual DB/WAL/SHM descriptor count to zero.
    database.close_thread_db()
    baseline_slots = len(database._all_connections)
    baseline_fds = _database_fds(isolated_db)
    assert baseline_slots == baseline_fds == 0
    workers = queue.Queue()
    app = FastAPI()

    @app.get("/probe")
    def probe():
        db = database.get_db()
        workers.put((threading.current_thread(), db._conn))
        db.execute("INSERT INTO http_lifetime_probe VALUES ('pending')")
        if fail:
            raise ValueError("synthetic HTTP failure")
        db.close()  # Borrower close rolls back but leaves the worker slot usable.
        return {"result": db.execute("SELECT 1").fetchone()[0]}

    for _ in range(12):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/probe")
            assert response.status_code == (500 if fail else 200)
            if not fail:
                assert response.json() == {"result": 1}
        thread, raw = workers.get_nowait()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(database._all_connections) == baseline_slots
        assert _database_fds(isolated_db) == baseline_fds
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            raw.execute("SELECT 1")
        connection = database.get_db()
        assert connection.execute("SELECT count(*) FROM http_lifetime_probe").fetchone()[0] == 0
        database.close_thread_db()


def test_cancelled_anyio_caller_keeps_connection_until_worker_finishes(isolated_db):
    baseline = len(database._all_connections)
    started = threading.Event()
    release = threading.Event()
    workers = queue.Queue()

    def work():
        db = database.get_db()
        workers.put((threading.current_thread(), db._conn))
        started.set()
        assert release.wait(timeout=5)
        assert db.execute("SELECT 1").fetchone()[0] == 1

    async def cancel_caller():
        task = asyncio.create_task(anyio.to_thread.run_sync(work, abandon_on_cancel=True))
        try:
            async with asyncio.timeout(5):
                while not started.is_set():
                    await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(database._all_connections) == baseline + 1
        finally:
            release.set()

    asyncio.run(cancel_caller())
    thread, raw = workers.get_nowait()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(database._all_connections) == baseline
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        raw.execute("SELECT 1")
