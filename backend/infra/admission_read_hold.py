"""Read-only facts scoped to one input-admission write frame."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
import sqlite3


@dataclass
class _Hold:
    connection: Any
    shapes: dict[str, sqlite3.Cursor] = field(default_factory=dict)
    aggregate_ready: bool | None = None


_current: ContextVar[_Hold | None] = ContextVar("admission_read_hold", default=None)


@contextmanager
def hold(connection):
    # Input-admission holds do not execute DDL.
    token = _current.set(_Hold(connection))
    try:
        yield
    finally:
        _current.reset(token)


def current(connection):
    value = _current.get()
    return value if value is not None and value.connection is connection else None
