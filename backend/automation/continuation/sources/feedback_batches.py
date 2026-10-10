"""Durable row-timer source for document feedback batches."""

from __future__ import annotations

from doceditor import feedback_batches


class FeedbackBatchSource:
    """Close due feedback rows before ordinary continuation collection."""

    kind = "document_feedback_batch"

    def prepare(self, now: str, *, db) -> list[str]:
        """Deliver due rows and return newly selected recipient threads to drive."""
        return feedback_batches.close_due(now, db=db)

    def due(self, _now: str, *, db) -> list:
        """Return no continuation triggers; plan admission owns recipient wakes."""
        return []

    def settle(self, *_args, **_kwargs) -> None:
        """No-op because prepare settles each durable batch row itself."""
