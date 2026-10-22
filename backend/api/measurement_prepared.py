"""Prepared Measurement tables, and the single-flight that shares their preparation.

``api.measurement_queries`` owns what the tables contain; this module owns their
names and DDL, and lets concurrent same-key requests share one preparation.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from api.measurement_scope import query_params

PREPARED_SCOPE = "measurement_scope_threads"
PREPARED_OUTCOMES = "measurement_outcome_periods"
PREPARED_ROOTS = "measurement_scope_roots"
PREPARED_ROOT_OUTCOMES = "measurement_root_outcome_periods"
PREPARED_ROOT_DIFFICULTIES = "measurement_root_difficulty_periods"
PREPARED_OUTCOME_NODES = "measurement_outcome_nodes"

DDL = {
    PREPARED_SCOPE: "CREATE TEMP TABLE IF NOT EXISTS measurement_scope_threads (id TEXT PRIMARY KEY, root_id TEXT)",
    PREPARED_OUTCOMES: """
        CREATE TEMP TABLE IF NOT EXISTS measurement_outcome_periods (
            period TEXT PRIMARY KEY, outcomes INTEGER NOT NULL, first_try INTEGER NOT NULL,
            rejections INTEGER NOT NULL
        )
    """,
    PREPARED_ROOTS: "CREATE TEMP TABLE IF NOT EXISTS measurement_scope_roots (id TEXT PRIMARY KEY)",
    PREPARED_OUTCOME_NODES: """
        CREATE TEMP TABLE IF NOT EXISTS measurement_outcome_nodes (
            node_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, period TEXT NOT NULL,
            first_try INTEGER NOT NULL, difficulty TEXT
        )
    """,
    PREPARED_ROOT_OUTCOMES: """
        CREATE TEMP TABLE IF NOT EXISTS measurement_root_outcome_periods (
            root_id TEXT NOT NULL, period TEXT NOT NULL, outcomes INTEGER NOT NULL,
            first_try INTEGER NOT NULL, PRIMARY KEY (root_id, period)
        )
    """,
    PREPARED_ROOT_DIFFICULTIES: """
        CREATE TEMP TABLE IF NOT EXISTS measurement_root_difficulty_periods (
            root_id TEXT NOT NULL, period TEXT NOT NULL, difficulty TEXT NOT NULL,
            count INTEGER NOT NULL, PRIMARY KEY (root_id, period, difficulty)
        )
    """,
}
#: The accepted tasks' walked chains, filled by ``api.measurement_model_cost.materialize_chain``; not shared.
CHAIN_TABLE = "temp.measurement_task_chain"
CHAIN_DDL = ("CREATE TEMP TABLE IF NOT EXISTS measurement_task_chain "
             "(task_id TEXT, period TEXT, settled_at TEXT, node_id TEXT)")
_SCOPE_TABLES = (PREPARED_SCOPE, PREPARED_OUTCOMES, PREPARED_OUTCOME_NODES)
_ROOT_TABLES = (*_SCOPE_TABLES, PREPARED_ROOTS, PREPARED_ROOT_OUTCOMES, PREPARED_ROOT_DIFFICULTIES)


def create_tables(db, roots: bool) -> None:
    """Create the prepared tables a preparation fills, before its first write.

    sqlite3 opens a transaction at a job's first write, and the pooled connection's close rolls it back.
    DDL inside that transaction is rolled back with it, so the table would be created again on every
    request, and rolling back DDL makes SQLite discard the connection's whole parsed schema: the next
    statement on that connection, whichever job sends it, re-reads every table, index and trigger.
    Created first, the tables persist on the pooled connection and ``IF NOT EXISTS`` is a no-op after.
    The chain table is created here too, as a job that reads chains may write these tables first.
    """
    for table in _ROOT_TABLES if roots else _SCOPE_TABLES:
        db.execute(DDL[table])
    db.execute(CHAIN_DDL)


# --- Single-flight preparation ---------------------------------------------------
#
# One Measurement page load asks for nine panels at once and each request used to
# rebuild the identical scope and outcome tables. Requests stay independent (each
# panel still succeeds, fails and renders alone); same-key callers that arrive
# while a preparation is running wait for it and copy its result. Nothing is kept
# once it finishes: a caller arriving afterwards prepares fresh, so there is no
# cache to invalidate. A waiter may receive tables the leader began reading just
# before the waiter arrived, so it can miss a write committed in that instant.
#
# The key is the database file, whether the per-agent grains are built, and every
# bound parameter ``query_params`` produces (user, project, agent, window and its
# timestamp bounds), and that includes the page grain: its viewer zone sets every period
# boundary, so two zones never share. Keying on all of them rather than a hand-kept list
# means a parameter the preparation SQL starts to read can only over-key, which costs
# sharing, never correctness.
_flights_lock = threading.Lock()
_flights: dict[tuple, "_Flight"] = {}


class _Flight:
    """One in-progress preparation that same-key callers wait on."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.waiters = 0
        self.snapshot: dict[str, list[tuple]] | None = None


def _flight_key(db, params: dict[str, Any], roots: bool) -> tuple:
    database = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), "")
    return (database, roots, tuple(sorted(query_params(params).items())))


def _snapshot(db, roots: bool) -> dict[str, list[tuple]]:
    return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
            for table in (_ROOT_TABLES if roots else _SCOPE_TABLES)}


def _restore(db, snapshot: dict[str, list[tuple]]) -> None:
    create_tables(db, roots=PREPARED_ROOTS in snapshot)
    for table, rows in snapshot.items():
        db.execute(f"DELETE FROM {table}")
        if rows:
            db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)


def shared_prepare(db, params: dict[str, Any], roots: bool, materialize: Callable[[], None]) -> None:
    """Fill the prepared tables on ``db``, sharing one ``materialize`` among overlapping callers."""
    key = _flight_key(db, params, roots)
    flight = _Flight()
    with _flights_lock:
        running = _flights.get(key)
        if running is None:
            _flights[key] = flight
        else:
            running.waiters += 1
            flight = running
    if running is not None:
        flight.done.wait()
        if flight.snapshot is not None:
            _restore(db, flight.snapshot)
        else:
            # The preparation this caller waited on failed; run its own so it
            # reports its own outcome.
            materialize()
        return
    try:
        materialize()
        with _flights_lock:
            _flights.pop(key, None)
            shared = flight.waiters > 0
        # Callers arriving after the pop prepare for themselves, so the waiters
        # counted here are the only ones that read the copy.
        if shared:
            flight.snapshot = _snapshot(db, roots)
    finally:
        with _flights_lock:
            if _flights.get(key) is flight:
                del _flights[key]
        flight.done.set()
