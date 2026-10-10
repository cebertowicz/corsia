"""Marks the code that may write a plan node's aggregate inputs (#731 S1b-2).

The stored plan aggregates stay exact only while every write of an aggregate input
(INSERT or DELETE of a node row; ``state``, ``archived_at``, ``spawned_thread_id``,
``parent_id``, ``plan_id``, ``sent_back``) runs the hook. The sanctioned writers
enter :func:`sanctioned_write` around their statement. Production never reads the
flag; the test suite installs a SQLite authorizer (``tests/plan_writer_authorizer``)
that refuses an input write made outside it, so a raw writer fails where it is written.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

_ACTIVE: ContextVar[bool] = ContextVar("plan_node_sanctioned_writer", default=False)


def active() -> bool:
    """Whether the current context is inside a sanctioned plan-node writer."""
    return _ACTIVE.get()


@contextmanager
def sanctioned_write() -> Iterator[None]:
    """Mark the enclosed statements as a sanctioned plan-node write."""
    token = _ACTIVE.set(True)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def execute(connection: sqlite3.Connection, sql: str, parameters: Sequence = ()) -> sqlite3.Cursor:
    """Run one statement as a sanctioned plan-node write."""
    with sanctioned_write():
        return connection.execute(sql, parameters)
