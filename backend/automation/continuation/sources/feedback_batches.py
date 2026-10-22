"""Durable row-timer source for document feedback batches."""

from __future__ import annotations

from datetime import datetime

from doceditor import feedback_batches, feedback_state


class FeedbackBatchSource:
    """Close due feedback rows before ordinary continuation collection."""

    kind = "document_feedback_batch"

    def prepare(self, now: str, *, db) -> list[str]:
        """Deliver due rows and return newly selected recipient threads to drive."""
        return feedback_batches.close_due(now, db=db)

    def seconds_until_next_deadline(self, now: datetime, *, db) -> float | None:
        """Seconds until the earliest open batch still ahead of its deadline, for the loop's sleep."""
        return feedback_state.seconds_until_next_due(now, db=db)

    def due(self, _now: str, *, db) -> list:
        """Return no continuation triggers; plan admission owns recipient wakes."""
        return []

    def settle(self, *_args, **_kwargs) -> None:
        """No-op because prepare settles each durable batch row itself."""
