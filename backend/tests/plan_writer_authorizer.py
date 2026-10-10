"""The dynamic layer of the plan-aggregates write gate, for the test suite (#731 S1b-2).

``tests/test_plan_aggregate_input_writers.py`` is the static layer. This is the
backstop for what a static scan cannot see: ``conftest.py`` calls :func:`install`
before any test connection exists, and every connection ``sqlite3.connect``
opens afterward carries an authorizer that refuses

* INSERT or DELETE of a ``plan_nodes`` row, and
* UPDATE of a ``plan_nodes`` aggregate input (``state``, ``archived_at``,
  ``spawned_thread_id``, ``parent_id``, ``plan_id``, ``sent_back``),

unless the sanctioned writers marked the context (``plans.writer_context``). A
test that needs rows in a shape the real operations cannot reach seeds them in
:func:`raw_plan_seed`, which recomputes every plan's aggregates on the way out.

The refusal reaches the caller as ``PlanWriterRefused`` (a ``sqlite3.DatabaseError``)
naming the routes, because SQLite itself reports only "not authorized".

:func:`recount_changed_plans` is the third layer: after each test, every plan on
a connection that wrote during it is compared with the recount oracle.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import weakref
from collections.abc import Iterator
from contextlib import closing, contextmanager
from typing import Any
from urllib.parse import quote

from plans import aggregates, writer_context
from plans.aggregates_rebuild import rebuild
from plans.aggregates_recount import stored_mismatches

# Its wrapper frames sit between a test and ``infra.db_connection``; the producer
# attribution of a pooled cursor must skip them and name the test line.
__producer_transparent__ = True

INPUT_COLUMNS = frozenset(
    {"state", "archived_at", "spawned_thread_id", "parent_id", "plan_id", "sent_back"}
)
_ROUTES = (
    "write a node through the real plan operation (plans.service / plans.node_creation), "
    "or seed rows inside `with raw_plan_seed(connection):` (tests/plan_writer_authorizer.py), "
    "which runs aggregates.rebuild for every plan on exit"
)

_pending = threading.local()


class PlanWriterRefused(sqlite3.DatabaseError):
    """A plan-node aggregate input was written outside a sanctioned writer."""


def _refusal(action: int, column: str | None) -> str | None:
    if action == sqlite3.SQLITE_INSERT:
        return "INSERT INTO plan_nodes"
    if action == sqlite3.SQLITE_DELETE:
        return "DELETE FROM plan_nodes"
    if action == sqlite3.SQLITE_UPDATE and column in INPUT_COLUMNS:
        return f"UPDATE plan_nodes SET {column}"
    return None


def authorize(action: int, table: str | None, column: str | None, *_unused: Any) -> int:
    """SQLite authorizer: deny an aggregate-input write outside a sanctioned writer."""
    if table != "plan_nodes" or writer_context.active():
        return sqlite3.SQLITE_OK
    what = _refusal(action, column)
    if what is None:
        return sqlite3.SQLITE_OK
    _pending.message = f"{what} outside a sanctioned writer: {_ROUTES}"
    return sqlite3.SQLITE_DENY


def _translate(error: sqlite3.DatabaseError) -> sqlite3.DatabaseError:
    message = getattr(_pending, "message", None)
    if message is None or "not authorized" not in str(error):
        return error
    _pending.message = None
    return PlanWriterRefused(message)


def _translating(call: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a statement; an authorizer refusal surfaces as ``PlanWriterRefused``."""
    try:
        return call(*args, **kwargs)
    except sqlite3.DatabaseError as error:
        translated = _translate(error)
        if translated is error:
            raise
        raise translated from error


class _GuardedCursor(sqlite3.Cursor):
    def execute(self, *args, **kwargs):
        return _translating(super().execute, *args, **kwargs)

    def executemany(self, *args, **kwargs):
        return _translating(super().executemany, *args, **kwargs)

    def executescript(self, *args, **kwargs):
        return _translating(super().executescript, *args, **kwargs)


_TRACKED: weakref.WeakKeyDictionary[sqlite3.Connection, int] = weakref.WeakKeyDictionary()
_GUARDED: dict[type, type] = {}
#: Per connection, the thread that opened it and the database file (None for memory or a URI).
_OWNER: weakref.WeakKeyDictionary[sqlite3.Connection, tuple[int, str | None]] = weakref.WeakKeyDictionary()


def _guarded(base: type) -> type:
    """``base`` with cursors that name the route when the authorizer refuses a write."""
    if base not in _GUARDED:
        class Guarded(base):  # type: ignore[valid-type, misc]
            def cursor(self, factory=sqlite3.Cursor):
                return super().cursor(_GuardedCursor if factory is sqlite3.Cursor else factory)

            # Each keeps the base class's own override (a test's observing connection).
            def execute(self, *args, **kwargs):
                return _translating(super().execute, *args, **kwargs)

            def executemany(self, *args, **kwargs):
                return _translating(super().executemany, *args, **kwargs)

            def executescript(self, *args, **kwargs):
                return _translating(super().executescript, *args, **kwargs)

        Guarded.__name__ = f"Guarded{base.__name__}"
        _GUARDED[base] = Guarded
    return _GUARDED[base]


_original_connect = sqlite3.connect


def _file_path(args: tuple, kwargs: dict) -> str | None:
    target = args[0] if args else kwargs.get("database")
    if isinstance(target, (str, os.PathLike)) and not kwargs.get("uri"):
        text = os.fspath(target)
        return None if isinstance(text, bytes) or text in ("", ":memory:") else os.path.abspath(text)
    return None


def _connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    base = kwargs.get("factory") or sqlite3.Connection
    if len(args) <= 5:
        kwargs["factory"] = _guarded(base)
    connection = _original_connect(*args, **kwargs)
    connection.set_authorizer(authorize)
    _TRACKED[connection] = 0
    _OWNER[connection] = (threading.get_ident(), _file_path(args, kwargs))
    return connection


def install() -> None:
    """Route every ``sqlite3.connect`` through the authorizer; idempotent."""
    if sqlite3.connect is not _connect:
        sqlite3.connect = _connect  # type: ignore[assignment]


@contextmanager
def raw_plan_seed(connection: sqlite3.Connection) -> Iterator[None]:
    """Write ``plan_nodes`` rows the real operations cannot reach, then recompute aggregates.

    For a fixture that needs a shape the service now refuses (a failed node over open
    work, a settled parent with a child) or rows seeded faster than operations could.
    On exit every plan's aggregates are rebuilt, so the teardown recount judges the
    rows and not the seeding. A database below the aggregates schema (a migration
    fixture) has nothing to rebuild.
    """
    with writer_context.sanctioned_write():
        yield
        if aggregates.ready(connection):
            row_factory = connection.row_factory
            connection.row_factory = sqlite3.Row
            try:
                for plan in [row[0] for row in connection.execute("SELECT id FROM plans")]:
                    rebuild(connection, plan)
            finally:
                connection.row_factory = row_factory


def _mismatches(connection: sqlite3.Connection) -> list[str]:
    if not aggregates.ready(connection):
        return []
    row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        return [
            f"{plan}: {name}"
            for plan in [row[0] for row in connection.execute("SELECT id FROM plans")]
            for name in stored_mismatches(connection, plan)
        ]
    finally:
        connection.row_factory = row_factory


def recount_changed_plans() -> list[str]:
    """Mismatches of every plan on a connection that wrote since the last call.

    A connection another thread opened may be mid-statement there (a job still running
    as the test ends), and a read on it from here can deadlock against that job's own
    database lock until the busy timeout. Those are judged through a fresh read-only
    connection to the committed file instead, and skipped when the file is busy or the
    database has none.
    """
    found: list[str] = []
    for connection in list(_TRACKED):
        try:
            changes = connection.total_changes
            if changes == _TRACKED.get(connection, 0):
                continue
            _TRACKED[connection] = changes
            owner, path = _OWNER.get(connection, (threading.get_ident(), None))
            if owner == threading.get_ident():
                found.extend(_mismatches(connection))
            elif path is not None:
                uri = f"file:{quote(path)}?mode=ro"
                with closing(_original_connect(uri, uri=True, timeout=0, factory=_guarded(sqlite3.Connection))) as reader:
                    found.extend(_mismatches(reader))
        except sqlite3.Error:
            continue  # closed, locked or without the plan tables: nothing to compare
    return found
