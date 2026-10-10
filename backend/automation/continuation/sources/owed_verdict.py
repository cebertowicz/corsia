"""Returned delegated steps whose verdict is owed, noticed on the continuation runner's clock.

Like ``SilentChildSource``, this source raises no triggers of its own: the
reminder is written by ``plans.delegation_owed_verdict.notify_owed_verdicts``
through the one parent-wake writer, and the runner lends only its cadence.
"""

from __future__ import annotations

from automation.continuation.triggers import Trigger, TriggerRequest
from infra.database import get_db
from plans import public


class OwedVerdictSource:
    """Remind a parent once that a returned worker still awaits its verdict."""

    kind = "owed_verdict"

    def prepare(self, now: str, *, db) -> list[str]:
        """Return the parents reminded of an owed verdict, for the runner to drive.

        The candidates are read first, outside any write: a sweep that finds
        no owed verdict never takes the database writer (#2748). The runner's
        own sweep passes ``db=None``, so the read takes the pooled connection
        itself; only the write below owns a transaction.
        """
        read_db = db if db is not None else get_db()
        candidates = public.owed_verdicts(read_db, now=now)
        if not candidates:
            return []
        woken: list[str] = []
        with (
            public.plan_transaction(db) as connection,
            public.collect_wake_recipients(connection, woken),
        ):
            public.notify_owed_verdicts(
                connection, now=now, node_ids=[str(row["node_id"]) for row in candidates],
            )
        return woken

    def due(self, _now: str, *, db) -> list[TriggerRequest]:
        """Raise nothing: the reminder is written in ``prepare``."""
        return []

    def settle(self, _trigger: Trigger, _outcome: str, _reason: str, *, db) -> None:
        """Settle nothing, since this source never owns a trigger row."""
