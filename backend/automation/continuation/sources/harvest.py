"""Delegated-work returns, committed on the continuation runner's clock.

This source raises no triggers of its own. Every parent wake about a worker
goes through ``plans.delegation_notice._wake_parent``, and the return itself
through ``plans.plan_wake.notify_returned``. What the runner lends this source
is its cadence: each pass first commits the harvests that are ready.
"""

from __future__ import annotations

from automation.continuation.triggers import Trigger, TriggerRequest
from plans import public


class HarvestSource:
    """Commit delegated-child harvests before trigger discovery begins."""

    kind = "delegation_harvest"

    def prepare(self, _now: str, *, db) -> list[str]:
        """Return the parents these harvests woke.

        The return wake is appended here, so the runner starts each parent's
        Turn once this has committed.
        """
        woken: list[str] = []
        changed_plan_ids: list[str] = []
        with public.collect_touched() as touched:
            public.harvest_ready_children(
                db=db, drives=woken, changed_plan_ids=changed_plan_ids,
            )
        changed_plan_ids.extend(touched)
        public.notify_after_commit(changed_plan_ids, db=db)
        return woken

    def due(self, _now: str, *, db) -> list[TriggerRequest]:
        """Raise nothing: a harvest's wake is written by the harvest itself."""
        return []

    def settle(self, _trigger: Trigger, _outcome: str, _reason: str, *, db) -> None:
        """Settle nothing, since this source never owns a trigger row."""
