"""Silent delegated workers, noticed on the continuation runner's clock.

Like ``HarvestSource``, this source raises no triggers of its own: the wake is
written by ``plans.delegation_child_lifecycle.notify_silent_children`` through
the one parent-wake writer, and the runner lends only its cadence.
"""

from __future__ import annotations

from automation.continuation.triggers import Trigger, TriggerRequest
from infra.database import get_db
from plans import public


class SilentChildSource:
    """Wake a parent whose worker's running Turn has gone silent."""

    kind = "silent_child"

    def prepare(self, now: str, *, db) -> list[str]:
        """Return the parents told about a silent worker, for the runner to drive.

        The candidates are read first, outside any write: a sweep that finds
        no untold silence never takes the database writer (#2748). The
        runner's own sweep passes ``db=None``, so the read takes the pooled
        connection itself; only the write below owns a transaction.
        """
        read_db = db if db is not None else get_db()
        candidates = public.silent_children(read_db, now=now)
        if not candidates:
            return []
        woken: list[str] = []
        with (
            public.plan_transaction(db) as connection,
            public.collect_wake_recipients(connection, woken),
        ):
            public.notify_silent_children(
                connection, now=now, turn_ids=[str(row["turn_id"]) for row in candidates],
            )
        return woken

    def due(self, _now: str, *, db) -> list[TriggerRequest]:
        """Raise nothing: the wake is written in ``prepare``."""
        return []

    def settle(self, _trigger: Trigger, _outcome: str, _reason: str, *, db) -> None:
        """Settle nothing, since this source never owns a trigger row."""
