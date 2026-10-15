from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator

import infra.write_admission as write_admission  # mypy resolves a new submodule only this way
from infra import write_effects
from infra.db_loop_guard import guard_write_off_event_loop
from infra.config import DB_PATH
from infra.db_connection import (
    _NoOpClose, PooledConnection, ThreadConnectionLifetime, apply_thread_lane, checkpoint_and_close, refuse_lane_write,
)

log = logging.getLogger(__name__)

_thread_local = threading.local()
_pool_lock = threading.Lock()
_all_connections: list[sqlite3.Connection] = []

CONNECTION_BUSY_TIMEOUT_MS = 5000
CONNECTION_BUSY_TIMEOUT_SECONDS = CONNECTION_BUSY_TIMEOUT_MS / 1000

# Temporary diagnosis for issue #1077: "database is locked" killed Codex bridge
# sessions with no record of the holder. In-process writers now queue in FIFO
# order on their database file's ticket (infra.write_admission), so a refusal is
# a ticket wait past its bound or a writer in another process, and the ticket
# names the holder. The refusal's errorcode/errorname tell SQLITE_BUSY,
# SQLITE_BUSY_SNAPSHOT and SQLITE_LOCKED apart; an admission refusal carries
# errorcode=None and a CORSIA_ADMISSION_* errorname. This slow-hold warning,
# _log_database_lock, write_admission.sqlite_errorname_tag (the bridge
# turn-error suffix) and write_admission._log_slow_wait are the evidence for that fix's acceptance:
# delete them in the change that settles Phase B's production acceptance (24 h
# with zero in-process lock refusals after deploy), and no later than
# 2026-10-23. Must never grow into retries, recovery, or suppression -- log only.
WRITE_TRANSACTION_SLOW_SECONDS = 1.0


def _finish_write_hold(scope: str, started_at: float | None) -> None:
    if started_at is None:  # BEGIN never succeeded, so nothing was held
        return
    held_for = time.monotonic() - started_at
    if held_for >= WRITE_TRANSACTION_SLOW_SECONDS:
        log.warning("SQLite write transaction held: scope=%s held=%.3fs", scope, held_for)


def is_lock_refusal(error: BaseException) -> bool:
    """Whether SQLite refused this write because another writer holds the lock.

    Classify on the exception type as well as the message: this codebase builds
    strings like "bridge error: OperationalError: database is locked" and can
    re-raise them as ordinary errors, so matching on text alone would report a
    downstream report of a lock as if it were fresh contention. By code, only
    the BUSY family names another writer; LOCKED is a table lock within one
    connection or shared cache that waiting never clears, so it is not one.
    """
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(error, sqlite3.OperationalError) and (
        "database is locked" in str(error).lower()
        or (isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_BUSY)
    )


def _log_database_lock(scope: str, error: BaseException) -> None:
    """Report lock contention, and only lock contention, with its holders."""
    if is_lock_refusal(error):
        log.error("SQLite database is locked: refused_scope=%s holders=%s errorcode=%s errorname=%s",
                  scope, write_admission.holders_summary(),
                  getattr(error, "sqlite_errorcode", None), getattr(error, "sqlite_errorname", None))


def _make_connection(path: str) -> sqlite3.Connection:
    """Open a new SQLite connection on ``path`` with standard settings."""
    conn = sqlite3.connect(path, check_same_thread=False, factory=PooledConnection)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={CONNECTION_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, faster than FULL
        conn.commit()
        apply_thread_lane(conn)
    except BaseException:
        conn.close()
        raise
    with _pool_lock:
        _all_connections.append(conn)
    return conn


def get_db() -> sqlite3.Connection:
    """Return this thread's pooled connection, reopened if ``DB_PATH`` (read once) changed outside a transaction."""
    path = str(DB_PATH)
    conn = getattr(_thread_local, "conn", None)
    if conn is not None and getattr(_thread_local, "path", path) != path and not conn.in_transaction:
        close_thread_db()
        conn = None
    if conn is None:
        conn = _make_connection(path)
        _thread_local.conn, _thread_local.path = conn, path
        _thread_local.lifetime = ThreadConnectionLifetime(conn, _release_connection)
        log.debug("SQLite: new thread-local connection (thread %s)", threading.current_thread().name)
    return _NoOpClose(conn)


class TransactionOwnershipError(RuntimeError):
    """A frame claimed the outermost write boundary on an already-busy connection."""


class SchemaVersionError(RuntimeError):
    """The database is not the exact clean-cutover schema."""


def _rollback_thread_transaction() -> bool:
    """Rollback this thread's pooled transaction, if one is open."""
    connection = getattr(_thread_local, "conn", None)
    if connection is None or not connection.in_transaction:
        return False
    try:
        connection.rollback()
    except sqlite3.Error:
        close_thread_db()
        raise
    return True


@contextmanager
def db_job_boundary(scope: str) -> Iterator[None]:
    """Require one executor job to start and finish without owning a write."""
    if _rollback_thread_transaction():
        raise TransactionOwnershipError(
            f"{scope} inherited an open transaction from a prior DB job"
        )
    try:
        yield
    except BaseException:
        try:
            _rollback_thread_transaction()
        except sqlite3.Error:
            log.exception("failed to clear pooled transaction after %s", scope)
        raise
    if _rollback_thread_transaction():
        raise TransactionOwnershipError(
            f"{scope} returned with an uncommitted transaction; its writes were rolled back"
        )


@contextmanager
def write_transaction(
    db: sqlite3.Connection | None, *, scope: str,
) -> Iterator[sqlite3.Connection]:
    """Run one write transaction whose ownership is declared, not guessed.

    ``get_db()`` hands every caller on a thread the *same* connection, so "is a
    transaction open?" cannot answer "is it mine?".  Ownership is therefore
    declared by the ``db`` argument and enforced here:

    * ``db=None`` claims the outermost boundary: this frame owns the connection
      and the transaction, so it requires a clean connection and is the only
      frame that may ``BEGIN``, ``commit``, ``rollback``, or ``close``.  A
      pooled connection that is already in a transaction has an owner this
      frame cannot see; nesting under it and then closing would silently
      discard that owner's work, so it raises instead.
    * ``db=<connection>`` names the owner explicitly.  If that connection is
      already in a transaction, an enclosing frame on this call stack owns it:
      this frame nests with a ``SAVEPOINT`` and never commits, never rolls the
      outer transaction back, and never closes.  Because the owner is a caller
      that cannot commit while we are inside it, the savepoint is valid by
      construction rather than by convention.
    * ``db=<clean connection>`` delegates the transaction but not the
      connection: this frame begins and commits, and leaves the connection open
      for its owner.
    ``scope`` names the savepoint so a failure identifies the frame that opened
    it.  It is interpolated into SQL, so it must be a bare identifier; callers
    pass a literal, and this keeps that provable at the one place it is used.
    """
    if not scope.isidentifier():
        raise ValueError(f"transaction scope must be a bare identifier: {scope!r}")
    connection = db if db is not None else get_db()
    refuse_lane_write(connection, scope)
    owns_connection = db is None
    if owns_connection and connection.in_transaction:
        raise TransactionOwnershipError(
            f"{scope} claimed the outermost write transaction, but this thread's "
            "pooled connection is already inside one owned by another frame; "
            "pass that owner's connection as db= instead"
        )
    try:
        if connection.in_transaction:
            savepoint = f"{scope}_{uuid.uuid4().hex}"
            connection.execute(f"SAVEPOINT {savepoint}")
            effects_mark = write_effects.enter(connection)
            try:
                yield connection
            except BaseException:
                write_effects.leave(connection, effects_mark, ok=False)
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            write_effects.run(write_effects.leave(connection, effects_mark, ok=True))
            return
        started_at: float | None = None
        admission: write_admission.Admission | None = None
        committed: list[write_effects.Effect] = []
        try:
            guard_write_off_event_loop(scope)
            admission = write_admission.admit(connection, scope)
            connection.execute("BEGIN IMMEDIATE")
            write_effects.begin(connection)
            started_at = time.monotonic()
            yield connection
            connection.commit()
            committed = write_effects.take(connection)
        except BaseException:
            write_effects.abort(connection)
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            _finish_write_hold(scope, started_at)
            write_admission.release(admission)  # before effects, which may write
            if owns_connection:
                connection.close()
        write_effects.run(committed)
    except BaseException as error:
        _log_database_lock(scope, error)
        raise


def _release_connection(conn: sqlite3.Connection) -> None:
    """Claim physical close once, against thread exit, explicit close or shutdown."""
    with _pool_lock:
        if conn not in _all_connections:
            return
        _all_connections.remove(conn)
    try:
        conn.close()
    except sqlite3.Error as error:
        log.warning("thread-local conn close failed: %s", error)


def close_thread_db() -> None:
    """Explicitly close and remove the thread-local connection."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        _release_connection(conn)
        _thread_local.conn = None
        _thread_local.lifetime = None


def shutdown_pool(reconfigure: Callable[[], None] | None = None) -> None:
    """Close the pool and optionally install its replacement under one admission boundary."""
    from infra.db_async import close_pool

    def close() -> None:
        global _thread_local
        with _pool_lock:
            conns = list(_all_connections)
            _all_connections.clear()
        checkpoint_and_close(conns)
        _thread_local = threading.local()
        if reconfigure is not None:
            reconfigure()
        log.info("SQLite pool shut down (%d connections closed, WAL checkpointed)", len(conns))

    close_pool(close)


def initialize_schema(conn: sqlite3.Connection) -> bool:
    """Create a fresh schema, or migrate an existing one forward to current.

    Returns True only when a fresh schema was created. An existing database at
    an older version is migrated in place rather than refused, so a schema
    change does not make the next deploy wait on a hand-run converter. A
    database this build cannot reach — no registered migration, or written by a
    newer build — is still refused rather than guessed at.
    """
    from infra.database_migrations import MigrationError, migrate_to_current
    from infra.schema import SCHEMA_SQL, SCHEMA_VERSION

    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None
    if has_tables:
        if version != SCHEMA_VERSION:
            try:
                migrate_to_current(conn, SCHEMA_VERSION)
            except MigrationError as error:
                raise SchemaVersionError(str(error)) from error
        return False
    if version not in {0, SCHEMA_VERSION}:
        raise SchemaVersionError(
            f"empty database version {version} is not {SCHEMA_VERSION}; "
            "run the external database converter before startup"
        )
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL)
        _init_fts(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return True


def init_db():
    """Create or open the exact current schema; never upgrade an older one."""
    conn = get_db()
    initialize_schema(conn)

    conn.close()

    from connections import seed_builtin_connections
    seed_builtin_connections()

    from infra.subagent_caps import seed_subagent_caps
    seed_subagent_caps()

    from infra.config import WORKSPACE
    from infra.corsia_agent_role_files import carry_workspace_conductor_role_files
    carry_workspace_conductor_role_files(WORKSPACE)


def _init_fts(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual tables and sync triggers."""
    from infra.db_fts import _init_fts as _fts
    _fts(conn)


def init_model_profiles():
    """Create model_profiles table if needed."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_profiles (
            model_id TEXT PRIMARY KEY,
            context_window INTEGER,
            temperature REAL,
            parallel_slots INTEGER DEFAULT 1,
            reasoning_budget INTEGER DEFAULT -1,
            flash_attention INTEGER DEFAULT 1,
            custom_args TEXT,
            max_tokens INTEGER,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()
