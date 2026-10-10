"""The plan write transaction: one write and the status marker it leaves its owner."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Iterator, Mapping

from infra.database import write_transaction
from infra.write_effects import connection_key


#: Connections with a plan write open on this call stack, each with the time
#: its outermost write began. A nested plan write is inside its caller's
#: window, so only the outermost one marks.
_OPEN_WRITES: ContextVar[Mapping[int, str]] = ContextVar(
    "plan_open_writes", default=MappingProxyType({}),
)


def write_started(connection) -> str | None:
    """When the plan write open on ``connection`` on this call stack began, if one is."""
    return _OPEN_WRITES.get().get(connection_key(connection))


@contextmanager
def transaction(db) -> Iterator[sqlite3.Connection]:
    """One plan write, and the marker that its owner's status is owed, in one transaction.

    Every plan writer goes through here. After the write and before its
    commit, each thread plan the transaction touched is marked dirty
    (``status_context.mark_touched``): the write and the marker are one
    durable fact. The status projection is not computed under the writer
    lock; ``plans.status_drain`` journals it once the write commits, and a
    digest built for the owner first drains whatever is still owed.
    """
    from plans import service, status_context, status_drain

    with write_transaction(db, scope="plan_service") as connection:
        open_writes = _OPEN_WRITES.get()
        key = connection_key(connection)
        if key in open_writes:
            yield connection
            return
        started = service._now()
        outer = _OPEN_WRITES.set(MappingProxyType({**open_writes, key: started}))
        try:
            yield connection
        finally:
            _OPEN_WRITES.reset(outer)
        touched = status_context.mark_touched(connection, since=started)
        if touched:
            from plans import notifications

            status_drain.schedule(connection, touched)
            notifications.report_touched(touched, db=connection)
