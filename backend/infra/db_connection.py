"""Borrowed SQLite handles and their owning thread's resource lifetime."""

import logging
import os
import sqlite3
import sys
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)

_jobs = threading.local()
# The executor lane a DB worker thread belongs to, set once when the thread
# starts. Kept apart from ``infra.database._thread_local``, which pool resets
# and test fixtures replace, so a lane thread's next connection is still opened
# for its lane.
_lane = threading.local()


def mark_thread_lane(lane: str) -> None:
    """Bind the current executor thread to a read-only lane (``read`` or ``bulk``)."""
    _lane.name = lane


def thread_lane() -> str | None:
    """The read-only lane this thread serves, or None for the default lane and every other thread."""
    return getattr(_lane, "name", None)


class LaneWriteError(RuntimeError):
    """A write transaction was opened on a read or bulk lane's read-only connection."""


# Statements a bulk-lane connection may run besides reading: a report prepares
# its data in TEMP tables, which ``query_only`` would refuse.
_BULK_READS = frozenset({
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE,
    sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
    sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_TABLE, sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_VIEW, sqlite3.SQLITE_DROP_TEMP_TRIGGER,
})
_ROW_WRITES = frozenset({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE})
# PRAGMAs that only report. Each may name a table or index; none may be assigned.
_READ_PRAGMAS = frozenset({
    "database_list", "table_list", "table_info", "table_xinfo", "index_list", "index_info",
    "index_xinfo", "foreign_key_list", "compile_options", "data_version", "page_count",
    "page_size", "freelist_count", "schema_version", "user_version",
})
_NAMED_PRAGMAS = frozenset({
    "table_info", "table_xinfo", "index_list", "index_info", "index_xinfo", "foreign_key_list",
})


def _bulk_authorizer(action: int, arg1: str | None, arg2: str | None, db_name: str | None, _source) -> int:
    """Allow reads and TEMP writes; deny main writes, ATTACH/DETACH, other DDL and writing PRAGMAs."""
    if action in _BULK_READS:
        return sqlite3.SQLITE_OK
    if action in _ROW_WRITES:
        return sqlite3.SQLITE_OK if db_name == "temp" else sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        name = (arg1 or "").lower()
        if name in _READ_PRAGMAS and (arg2 is None or name in _NAMED_PRAGMAS):
            return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def apply_thread_lane(conn: "PooledConnection") -> None:
    """Make a new pooled connection read-only when its thread serves the read or bulk lane.

    The read lane sets ``query_only``. The bulk lane installs an authorizer
    instead, so a report can still build TEMP tables while any write to the main
    database fails at prepare. Neither stops ``BEGIN IMMEDIATE`` from taking the
    write lock on the bulk lane, so :func:`refuse_lane_write` guards that.
    """
    lane = thread_lane()
    if lane == "read":
        conn.execute("PRAGMA query_only=ON")
    elif lane == "bulk":
        conn.set_authorizer(_bulk_authorizer)
    conn.db_lane = lane


def refuse_lane_write(conn: sqlite3.Connection, scope: str) -> None:
    """Refuse a write transaction on a read or bulk lane's pooled connection."""
    lane = getattr(conn, "db_lane", None)
    if lane is not None:
        raise LaneWriteError(
            f"write transaction {scope} refused: the {lane} lane's connection is read-only; "
            "run this job with run_in_db"
        )


def checkpoint_and_close(conns: list[sqlite3.Connection]) -> None:
    """Checkpoint the WAL through every pooled connection, then close each one.

    Admission is closed, so no job is running, and the bulk lane's authorizer,
    which denies the checkpoint PRAGMA, is lifted first.
    """
    for conn in conns:
        try:
            conn.set_authorizer(None)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as error:
            log.warning("wal_checkpoint on shutdown failed: %s", error)
    for conn in conns:
        try:
            conn.close()
        except sqlite3.Error as error:
            log.warning("conn.close() on shutdown failed: %s", error)


class PooledConnection(sqlite3.Connection):
    """A pool slot's connection whose cursors a running DB job can account for.

    A cursor whose statement is still stepping pins this connection's read
    snapshot. If it outlives its job, for example as a local of a frame held
    by the job's propagating exception, every later job on the pool thread
    reads stale rows and cannot begin a write (SQLITE_BUSY_SNAPSHOT).
    ``Connection.execute`` and its siblings do not call an overridden
    ``cursor()``, so they are routed through it here.
    """

    db_lane: str | None = None  # set by apply_thread_lane on a read or bulk lane

    def cursor(self, factory=sqlite3.Cursor):
        cursor = super().cursor(factory)
        cursors = getattr(_jobs, "cursors", None)
        if cursors is not None:
            cursors[cursor] = _producer(sys._getframe(1))
        return cursor

    def execute(self, sql, parameters=(), /):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, script, /):
        return self.cursor().executescript(script)


def _producer(frame) -> tuple:
    while frame is not None and frame.f_code.co_filename == __file__:
        frame = frame.f_back
    return (frame.f_code, frame.f_lineno) if frame is not None else (None, 0)


def _describe(producer: tuple) -> str:
    code, line = producer
    if code is None:
        return "unknown"
    path = code.co_filename.replace(os.sep, "/")
    return f"{path.rpartition('/backend/')[2] or path}:{code.co_qualname}:{line}"


@contextmanager
def job_cursors(scope: str) -> Iterator[None]:
    """Close every pooled cursor this job opened before the next job can run.

    Only cursors created on this thread's pooled connections while this job
    runs are owned; a nested job owns only its own. Private and caller-owned
    connections are never touched. A cursor escaping a job that returned is a
    contract violation: it is closed, so any later use raises, and logged. One
    held by a raised job's traceback is closed and, if its statement was still
    stepping, warned. Downgrade the raised-job WARNING to DEBUG, or delete
    it, once the producer named by this log is fixed, or after 14 days live
    with no WARNING. No SQL text or parameters are logged; they can carry
    private values.
    """
    outer = getattr(_jobs, "cursors", None)
    owned: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
    _jobs.cursors = owned
    try:
        yield
    except BaseException as error:
        _jobs.cursors = outer
        _close(owned, scope, raised=type(error).__name__)
        raise
    _jobs.cursors = outer
    _close(owned, scope, raised=None)


def _close(owned: weakref.WeakKeyDictionary, scope: str, *, raised: str | None) -> None:
    for cursor, producer in list(owned.items()):
        step_error = None
        try:
            # sqlite3 steps one row ahead and releases a statement at its last
            # row, so a finished SELECT, an empty one and DML return None here
            # without stepping; only a statement still holding a pending row
            # (and its snapshot) yields one. The cursor closes next, so the
            # probed row is not lost to anyone.
            query = cursor.fetchone() is not None
        except sqlite3.ProgrammingError:
            continue  # its connection or cursor is already closed: no snapshot remains
        except sqlite3.Error as failure:
            # The statement was still stepping; name the failure it hit.
            query, step_error = True, type(failure).__name__
        cursor.close()
        if raised is None and query:
            log.error(
                "SQLite cursor outlived its DB job and was closed to release its read "
                "snapshot: scope=%s producer=%s query=%s step_error=%s",
                scope, _describe(producer), query, step_error,
            )
        elif query:
            log.warning(
                "SQLite query cursor held by a raised DB job was closed to release its read "
                "snapshot: scope=%s producer=%s error=%s step_error=%s",
                scope, _describe(producer), raised, step_error,
            )


class _NoOpClose:
    """Connection wrapper whose close() rolls back without closing the pool slot."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)

    def close(self) -> None:
        """Clear any pending transaction so WAL checkpoints aren't blocked.

        Pooled SQLite connections can retain implicit DML transactions after an
        exception. rollback() releases that state; after commit() it is a no-op.
        """
        try:
            object.__getattribute__(self, "_conn").rollback()
        except sqlite3.Error as e:
            log.debug("rollback during close failed: %s", e)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)

    def __enter__(self):
        return object.__getattribute__(self, "_conn").__enter__()

    def __exit__(self, *args):
        return object.__getattribute__(self, "_conn").__exit__(*args)


class ThreadConnectionLifetime:
    """Release an exact pool slot when its thread-local lifetime ends.

    The pool tracks the raw connection, not this token. Clearing a thread's
    local dictionary therefore releases its slot even if raw handles or Thread
    objects are still referenced elsewhere. Explicit close and shutdown use the
    same owner callback, which claims each slot only once.

    Only the creating process may release. A fork clears the child's copies of
    other threads' locals before exec (uvloop spawns this way), and a child must
    never close the parent's SQLite handles: doing so can deadlock on a mutex a
    parent thread held at fork time, stranding the inherited listen socket.
    """

    def __init__(
        self, connection: sqlite3.Connection, release: Callable[[sqlite3.Connection], None],
    ) -> None:
        weakref.finalize(self, _release_in_owner, os.getpid(), release, connection)


def _release_in_owner(
    owner_pid: int, release: Callable[[sqlite3.Connection], None], connection: sqlite3.Connection,
) -> None:
    """Run release only in the process that opened the connection."""
    if os.getpid() == owner_pid:
        release(connection)
