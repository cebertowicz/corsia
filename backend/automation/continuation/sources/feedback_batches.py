"""Durable row-timer source for document feedback batches."""

from __future__ import annotations

from datetime import datetime

from doceditor import feedback_batches, feedback_state, writer_line
from infra.database import write_transaction


class FeedbackBatchSource:
    """Close due feedback rows before ordinary continuation collection."""

    kind = "document_feedback_batch"

    def prepare(self, now: str, *, db) -> list[str]:
        """Deliver due rows, serve comment writer lines left waiting, and return the threads to drive."""
        woken = feedback_batches.close_due(now, db=db)
        with write_transaction(db, scope="writer_line_sweep") as connection:
            return woken + writer_line.serve_waiting(connection)

    def seconds_until_next_deadline(self, now: datetime, *, db) -> float | None:
        """Seconds until the earliest open batch still ahead of its deadline, for the loop's sleep."""
        return feedback_state.seconds_until_next_due(now, db=db)

    def due(self, _now: str, *, db) -> list:
        """Return no continuation triggers; plan admission owns recipient wakes."""
        return []

    def settle(self, *_args, **_kwargs) -> None:
        """No-op because prepare settles each durable batch row itself."""
