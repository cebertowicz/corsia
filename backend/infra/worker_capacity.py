"""R23: a rule-started Claude Code worker at a full working-process limit waits for a slot.

The start makes the worker and holds its first turn at the FIFO head with cause
:data:`WORKER_CAPACITY` (``hold_unavailable_model_input``). That cause is neither a
model, locality nor account hold, so catalog recovery never touches it; only the
idle claim lifts it, through :func:`lift`, and only while there is a slot for it
after every older held start. The claim that lifts it inserts the worker's running
turn in the same write, and :func:`working` counts a running Claude Code turn with
no process yet, so one freed slot goes to exactly one held start, oldest first.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

WORKER_CAPACITY = "worker_capacity"


def working(db: sqlite3.Connection) -> int:
    """Claude Code processes working now, plus Claude Code turns running without one."""
    from subagents.cc_idle_retirement import working_thread_ids

    return len(working_thread_ids(db=db))


def full(db: sqlite3.Connection | None = None) -> bool:
    """Whether a new Claude Code worker would pass the working-process limit (on the
    calling DB job's connection when ``db`` is not given)."""
    from infra.database import get_db
    from infra.subagent_caps import get_max_concurrent_live

    return working(db if db is not None else get_db()) >= get_max_concurrent_live()


def lift(db: sqlite3.Connection, thread_id: str | None, rows: Iterable[sqlite3.Row]) -> frozenset[str]:
    """The worker-capacity holds among ``rows`` the idle claim may take now: only when the
    worker's rule owner is not paused and a slot is left after every older held start.
    Reads only: the worker runner marks the start running once its turn is."""
    held = frozenset(str(row["id"]) for row in rows if row["write_outcome"] == "proved_no_write"
                     and row["delivery_error"] == WORKER_CAPACITY)
    if not held or thread_id is None:
        return frozenset()
    from events import worker_starts
    from infra.subagent_caps import get_max_concurrent_live
    from plans.public import thread_paused_since

    start = worker_starts.started_by(db, thread_id)
    if start is None or thread_paused_since(db, start["thread_id"]) is not None:
        return frozenset()
    if get_max_concurrent_live() - working(db) <= worker_starts.held_before(db, thread_id):
        return frozenset()
    return held
