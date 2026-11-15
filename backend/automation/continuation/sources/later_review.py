"""Plans whose weekly Later review has come due, noticed on the continuation runner's clock.

Like ``OwedVerdictSource``, this source raises no triggers of its own: the wake
is written by ``plans.later_review`` through the one plan-wake writer, and the
runner lends only its cadence. Nothing here owns a row: the next review time
lives on the plan itself, so a restart loses no wake and none is queued twice.
"""

from __future__ import annotations

from automation.continuation.triggers import Trigger, TriggerRequest
from infra.database import get_db
from plans import public


class LaterReviewSource:
    """Wake a plan's owner once a week about the work it set aside."""

    kind = "later_review"

    def prepare(self, now: str, *, db) -> list[str]:
        """Return the owners woken for a due review, for the runner to drive.

        The candidates are read first, outside any write: a sweep that finds no
        due review never takes the database writer (#2748). The runner's own
        sweep passes ``db=None``, so the read takes the pooled connection
        itself; only the write below owns a transaction, and it re-checks each
        plan there, so a pause that lands between the two cancels that review.
        """
        read_db = db if db is not None else get_db()
        candidates = public.later_reviews_due(read_db, now=now)
        if not candidates:
            return []
        woken: list[str] = []
        with (
            public.plan_transaction(db) as connection,
            public.collect_wake_recipients(connection, woken),
        ):
            public.review_later_piles(
                connection, now=now, plan_ids=[str(row["id"]) for row in candidates],
            )
        return woken

    def due(self, _now: str, *, db) -> list[TriggerRequest]:
        """Raise nothing: the wake is written in ``prepare``."""
        return []

    def settle(self, _trigger: Trigger, _outcome: str, _reason: str, *, db) -> None:
        """Settle nothing, since this source never owns a trigger row."""
