"""FIFO write admission: writers on one database file take the write lock in arrival order."""

from __future__ import annotations

import asyncio
import gc
import os
import sqlite3
import threading
import time

import pytest

import infra.write_admission as write_admission
from infra import database, write_effects
from infra.database import write_transaction


@pytest.fixture
def pooled(tmp_path):
    """Open pooled connections, as ``get_db`` does, on one disposable database."""
    path = str(tmp_path / "admission.db")
    opened: list[sqlite3.Connection] = []

    def connect() -> sqlite3.Connection:
        connection = database._make_connection(path)
        opened.append(connection)
        return connection

    setup = connect()
    setup.execute("CREATE TABLE log(id INTEGER PRIMARY KEY, who TEXT)")
    setup.commit()
    yield connect
    for connection in opened:
        database._release_connection(connection)


def _ticket(connection) -> write_admission.WriteTicket:
    ticket = write_admission.ticket_of(connection)
    assert ticket is not None
    return ticket


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.001)


def _hold(connection, scope: str, ready: threading.Event, release: threading.Event) -> threading.Thread:
    def run() -> None:
        with write_transaction(connection, scope=scope):
            ready.set()
            assert release.wait(timeout=10)

    thread = threading.Thread(target=run, name=f"{scope}-thread")
    thread.start()
    assert ready.wait(timeout=5)
    return thread


def _rows(connection) -> list[str]:
    return [row[0] for row in connection.execute("SELECT who FROM log ORDER BY id")]


def test_pooled_connections_on_one_database_share_one_ticket(pooled, tmp_path) -> None:
    first, second = pooled(), pooled()
    other = database._make_connection(str(tmp_path / "other.db"))
    try:
        assert _ticket(first) is _ticket(second)
        assert _ticket(other) is not _ticket(first)
    finally:
        database._release_connection(other)


def test_writers_take_the_lock_in_arrival_order(pooled) -> None:
    holder = pooled()
    ticket = _ticket(holder)
    ready, release = threading.Event(), threading.Event()
    holding = _hold(holder, "holder_scope", ready, release)
    writers = []
    for index in range(6):
        connection = pooled()

        def write(connection=connection, index=index) -> None:
            with write_transaction(connection, scope=f"writer_{index}") as db:
                db.execute("INSERT INTO log(who) VALUES (?)", (f"writer_{index}",))

        writer = threading.Thread(target=write)
        writer.start()
        _wait_until(lambda index=index: len(ticket._waiters) == index + 1)
        writers.append(writer)
    release.set()
    for thread in [holding, *writers]:
        thread.join(timeout=10)
    assert _rows(holder) == [f"writer_{index}" for index in range(6)]
    assert ticket._holder is None and not ticket._waiters


def test_nested_savepoint_takes_no_ticket(pooled) -> None:
    connection = pooled()
    ticket = _ticket(connection)
    with write_transaction(connection, scope="outer_scope") as db:
        outer = ticket._holder
        assert outer is not None and outer.scope == "outer_scope"
        with write_transaction(db, scope="nested_scope"):
            assert ticket._holder is outer
            assert not ticket._waiters
        assert ticket._holder is outer
    assert ticket._holder is None


def test_ticket_is_released_when_the_body_raises(pooled) -> None:
    connection = pooled()
    with pytest.raises(RuntimeError, match="boom"):
        with write_transaction(connection, scope="failing_scope") as db:
            db.execute("INSERT INTO log(who) VALUES ('rolled back')")
            raise RuntimeError("boom")
    assert _ticket(connection)._holder is None
    assert _rows(connection) == []


def test_ticket_is_released_when_begin_fails(pooled, tmp_path) -> None:
    connection = pooled()
    connection.execute("PRAGMA busy_timeout=20")
    outside = sqlite3.connect(str(tmp_path / "admission.db"), isolation_level=None)
    try:
        outside.execute("BEGIN IMMEDIATE")  # another process's writer, as far as admission knows
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            with write_transaction(connection, scope="begin_fails"):
                pass
        assert _ticket(connection)._holder is None
    finally:
        outside.close()


def test_ticket_is_released_when_the_frame_is_closed_mid_write(pooled) -> None:
    connection = pooled()
    frame = write_transaction(connection, scope="abandoned_scope")
    frame.__enter__().execute("INSERT INTO log(who) VALUES ('abandoned')")
    assert _ticket(connection)._holder is not None
    frame.gen.close()  # what cancellation or garbage collection does to a suspended frame
    assert _ticket(connection)._holder is None
    assert not connection.in_transaction
    assert _rows(connection) == []


async def test_ticket_is_released_after_a_cancelled_db_job_finishes(isolated_db) -> None:
    from infra.db_async import run_in_db

    entered, release = threading.Event(), threading.Event()

    def job() -> None:
        with write_transaction(None, scope="cancelled_job") as db:
            entered.set()
            assert release.wait(timeout=10)
            db.execute("SELECT 1")

    task = asyncio.ensure_future(run_in_db(job))
    assert await asyncio.to_thread(entered.wait, 5)
    ticket = write_admission.ticket_for(str(isolated_db))
    assert ticket is not None and ticket._holder is not None
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ticket._holder is None


def test_after_commit_runs_after_release_and_may_write(pooled) -> None:
    connection = pooled()
    ticket = _ticket(connection)
    seen: list[object] = []

    def effect() -> None:
        seen.append(ticket._holder)
        with write_transaction(connection, scope="effect_write") as db:
            db.execute("INSERT INTO log(who) VALUES ('effect')")

    with write_transaction(connection, scope="registering_scope") as db:
        db.execute("INSERT INTO log(who) VALUES ('write')")
        write_effects.after_commit(db, effect)
    assert seen == [None]
    assert _rows(connection) == ["write", "effect"]
    assert ticket._holder is None


def test_wait_bounds_are_thirty_seconds_on_workers_and_five_on_the_loop() -> None:
    assert write_admission.WORKER_WAIT_SECONDS == 30.0
    assert write_admission.LOOP_WAIT_SECONDS == 5.0

    async def on_loop() -> float:
        return write_admission._wait_bound()

    assert write_admission._wait_bound() == write_admission.WORKER_WAIT_SECONDS
    assert asyncio.run(on_loop()) == write_admission.LOOP_WAIT_SECONDS


def test_worker_wait_past_its_bound_is_refused_naming_the_holder(pooled, monkeypatch, caplog) -> None:
    monkeypatch.setattr(write_admission, "WORKER_WAIT_SECONDS", 0.05)
    holder, waiter = pooled(), pooled()
    ticket = _ticket(holder)
    ready, release = threading.Event(), threading.Event()
    holding = _hold(holder, "holder_scope", ready, release)
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked") as refused:
            with write_transaction(waiter, scope="waiting_scope"):
                pass
    finally:
        release.set()
        holding.join(timeout=10)
    message = str(refused.value)
    assert "waiting_scope" in message and "waited 0.05s" in message
    assert "scope=holder_scope" in message and "thread=holder_scope-thread" in message
    assert "refused_scope=waiting_scope" in caplog.text
    assert "admission scope=holder_scope" in caplog.text
    assert "errorcode=None errorname=CORSIA_ADMISSION_TIMEOUT" in caplog.text
    assert refused.value.sqlite_errorname == "CORSIA_ADMISSION_TIMEOUT"
    assert ticket._holder is None and not ticket._waiters


def test_loop_thread_wait_uses_the_loop_bound(pooled, monkeypatch) -> None:
    monkeypatch.setattr(write_admission, "WORKER_WAIT_SECONDS", 10.0)
    monkeypatch.setattr(write_admission, "LOOP_WAIT_SECONDS", 0.05)
    holder = pooled()
    ticket = _ticket(holder)
    ready, release = threading.Event(), threading.Event()
    holding = _hold(holder, "holder_scope", ready, release)

    async def acquire_on_loop() -> float:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="holder_scope"):
            ticket.acquire("loop_scope")
        return time.monotonic() - started

    try:
        assert asyncio.run(acquire_on_loop()) < 5.0
    finally:
        release.set()
        holding.join(timeout=10)
    assert ticket._holder is None and not ticket._waiters


def test_a_waiter_that_times_out_leaves_the_queue_behind_it_intact(pooled, monkeypatch) -> None:
    bounds = {"impatient": 0.01, "patient": 10.0}
    monkeypatch.setattr(write_admission, "_wait_bound", lambda: bounds[threading.current_thread().name])
    holder = pooled()
    ticket = _ticket(holder)
    ready, release = threading.Event(), threading.Event()
    holding = _hold(holder, "holder_scope", ready, release)
    outcomes: dict[str, str] = {}

    def write(name: str) -> None:
        try:
            with write_transaction(pooled(), scope=name) as db:
                db.execute("INSERT INTO log(who) VALUES (?)", (name,))
            outcomes[name] = "written"
        except sqlite3.OperationalError as error:
            outcomes[name] = str(error)

    impatient = threading.Thread(target=write, args=("impatient",), name="impatient")
    patient = threading.Thread(target=write, args=("patient",), name="patient")
    impatient.start()
    _wait_until(lambda: len(ticket._waiters) == 1)
    patient.start()
    impatient.join(timeout=10)
    _wait_until(lambda: [w.scope for w in ticket._waiters] == ["patient"])
    release.set()
    for thread in (holding, patient):
        thread.join(timeout=10)
    assert "database is locked" in outcomes["impatient"]
    assert outcomes["patient"] == "written"
    assert _rows(holder) == ["patient"]
    assert ticket._holder is None and not ticket._waiters


def test_second_outermost_write_on_the_holding_thread_fails_at_once(pooled, caplog) -> None:
    first, second = pooled(), pooled()
    with write_transaction(first, scope="outer_scope") as db:
        db.execute("INSERT INTO log(who) VALUES ('outer')")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked: inner_scope opened a second"):
            with write_transaction(second, scope="inner_scope"):
                pass
        assert time.monotonic() - started < 1.0  # not SQLite's five-second busy wait
        assert "errorcode=None errorname=CORSIA_ADMISSION_NESTED" in caplog.text
    assert _rows(first) == ["outer"]
    assert _ticket(first)._holder is None


def _private(path) -> sqlite3.Connection:
    """A connection opened outside the pool, as a document store or setup lane does."""
    return sqlite3.connect(str(path), check_same_thread=False)


def test_private_writer_on_the_same_file_queues_in_order_with_pooled_writers(pooled, tmp_path) -> None:
    holder = pooled()
    ticket = _ticket(holder)
    ready, release = threading.Event(), threading.Event()
    holding = _hold(holder, "holder_scope", ready, release)
    writers = []
    for index, private in enumerate((True, False, True)):
        connection = _private(tmp_path / "admission.db") if private else pooled()

        def write(connection=connection, index=index) -> None:
            with write_transaction(connection, scope=f"writer_{index}") as db:
                db.execute("INSERT INTO log(who) VALUES (?)", (f"writer_{index}",))

        writer = threading.Thread(target=write)
        writer.start()
        _wait_until(lambda index=index: len(ticket._waiters) == index + 1)
        writers.append((writer, connection, private))
    release.set()
    for thread in [holding, *(writer for writer, _, _ in writers)]:
        thread.join(timeout=10)
    for _, connection, private in writers:
        if private:
            connection.close()
    assert _rows(holder) == ["writer_0", "writer_1", "writer_2"]
    assert ticket._holder is None and not ticket._waiters


def test_two_spellings_of_one_file_share_one_ticket(pooled, tmp_path) -> None:
    (tmp_path / "sub").mkdir()
    os.symlink(tmp_path, tmp_path / "link")
    canonical = pooled()
    spellings = [_private(tmp_path / "link" / "admission.db"), _private(tmp_path / "sub" / ".." / "admission.db")]
    try:
        assert all(_ticket(connection) is _ticket(canonical) for connection in spellings)
    finally:
        for connection in spellings:
            connection.close()


@pytest.mark.parametrize("path", [":memory:", ""])
def test_in_memory_and_temporary_databases_take_no_ticket(path) -> None:
    connection = sqlite3.connect(path)
    try:
        assert write_admission.ticket_of(connection) is None
        with write_transaction(connection, scope="unfiled_scope") as db:
            db.execute("CREATE TABLE t(x)")
            assert write_admission.holders_summary() == "none"
    finally:
        connection.close()


def test_private_second_outermost_write_on_the_holding_thread_fails_at_once(pooled, tmp_path) -> None:
    holder = pooled()
    inner = _private(tmp_path / "admission.db")
    try:
        with write_transaction(holder, scope="outer_scope"):
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match=r"inner_scope opened a second .*scope=outer_scope"):
                with write_transaction(inner, scope="inner_scope"):
                    pass
            assert time.monotonic() - started < 1.0  # was a self-deadlock until busy_timeout
    finally:
        inner.close()
    assert _ticket(holder)._holder is None


def test_an_idle_ticket_is_not_kept_once_its_writers_are_gone(tmp_path) -> None:
    path = tmp_path / "document.db"
    connection = _private(path)
    with write_transaction(connection, scope="document_scope") as db:
        db.execute("CREATE TABLE t(x)")
    connection.close()
    gc.collect()
    assert os.path.realpath(path) not in write_admission._tickets
