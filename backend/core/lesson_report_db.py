"""A private read snapshot never changes the pooled writer connection."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from collections.abc import Iterator
from threading import Event

from core.lesson_report_offers import LessonReportCollectionCancelled


@contextmanager
def report_snapshot(*, cancellation_event: Event | None = None) -> Iterator[sqlite3.Connection]:
    """Close the owned read transaction and connection on every exit path.

    A private connection can stop SQLite VM work when its caller has abandoned
    the report. This does not cover Python or render work after a SQL step.
    """
    from infra import database

    db = sqlite3.connect(database.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True,
                         timeout=database.CONNECTION_BUSY_TIMEOUT_SECONDS)
    try:
        db.row_factory = sqlite3.Row
        if cancellation_event is not None:
            db.set_progress_handler(cancellation_event.is_set, 1_000)
            if cancellation_event.is_set():
                raise LessonReportCollectionCancelled
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    except sqlite3.OperationalError as error:
        if (cancellation_event is not None and cancellation_event.is_set()
                and getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT):
            raise LessonReportCollectionCancelled from None
        raise
    finally:
        if cancellation_event is not None:
            db.set_progress_handler(None, 0)
        db.close()
