"""T2 expected-event watch expiry source for bounded continuation."""

from __future__ import annotations

from automation import plan_liveness, watches
from automation.continuation.triggers import Trigger, TriggerRequest
from plans import public


def _scope(thread_id: str, db) -> str:
    row = db.execute(
        "SELECT project_id FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    return row["project_id"] if row and row["project_id"] else "__workspace__"


def _transition(watch) -> str:
    return f"{watch.condition['type']}:{watch.condition.get('value') or '-'}"


def _task_open(node_id: str | None, db) -> bool:
    """Whether the task the watch was bound to can still resume; unbound is open."""
    if node_id is None:
        return True
    row = db.execute(
        "SELECT state FROM plan_nodes WHERE id=? AND archived_at IS NULL",
        (node_id,),
    ).fetchone()
    return row is not None and row["state"] not in public.TERMINAL_STATES


class WatchExpirySource:
    """Turn due external watches into one admitted T2 trigger each."""

    kind = "t2_watch_expiry"

    def due(self, now: str, *, db) -> list[TriggerRequest]:
        """Return all due deadline watches with an addressable owner."""

        requests = []
        for watch in watches.due_watches(now, db=db):
            if watch.owner_thread is None:
                continue
            # A scheduled task's wake names the task and why it was set aside
            # (W2), an event wait's says the event did not arrive (W4), a late
            # one says its time was missed (EW2); every other watch keeps the
            # one fixed summary.
            summary = (
                public.scheduled_wake(db, watch)
                or public.event_wake(db, watch)
                or public.late_wake(watch.condition.get("value"), now)
            )
            requests.append(
                TriggerRequest(
                    kind=self.kind,
                    thread_id=watch.owner_thread,
                    scope_id=_scope(watch.owner_thread, db),
                    subject_id=watch.id,
                    transition=_transition(watch),
                    reason="watch_expired",
                    details={
                        "watch_id": watch.id,
                        "plan_node_id": watch.plan_node_id,
                        **({"summary": summary} if summary else {}),
                    },
                )
            )
        return requests

    def is_current(
        self,
        thread_id: str,
        subject_id: str | None,
        transition: str | None,
        details: dict,
        *,
        db,
    ) -> bool:
        """Return whether the due watch is still this owner's armed one at append."""
        watch = self._owned(thread_id, details, db)
        return (
            watch is not None
            and watch.id == subject_id
            and watch.status == "armed"
            and transition == _transition(watch)
            and _task_open(watch.plan_node_id, db)
        )

    def still_actionable(self, thread_id: str, details: dict, *, db) -> bool:
        """Return whether the owner still has the task the expiry was about.

        Delivery expires the watch: that is what a delivered wake looks like,
        not a stale one. What makes it stale is the task itself, archived or
        settled between delivery and claim, leaving nothing to resume for.
        """
        watch = self._owned(thread_id, details, db)
        return watch is not None and _task_open(watch.plan_node_id, db)

    def _owned(self, thread_id: str, details: dict, db):
        try:
            watch = watches.get_watch(str(details.get("watch_id")), db=db)
        except watches.WatchError:
            return None
        return watch if watch.owner_thread == thread_id else None

    def settle(
        self,
        trigger: Trigger,
        outcome: str,
        reason: str,
        *,
        db,
    ) -> tuple[str, str, dict] | None:
        """Expire once, append a node audit, and re-arm T0 after settlement."""

        if trigger.subject_id is None or outcome == "deferred":
            return None
        watch = watches.get_watch(trigger.subject_id, db=db)
        expired = watches.expire_watch(trigger.subject_id, db=db)
        if watch.plan_node_id is not None:
            public.record_runner_event(
                watch.plan_node_id,
                "watch_expired",
                provenance="continuation.t2",
                payload={
                    "trigger_id": trigger.id,
                    "outcome": outcome,
                    "reason": reason,
                },
                db=db,
            )
            # Expiry clears a waiting cause, so Plans reconciles the watch's
            # plan here. Without it the task is eligible but selected by
            # nobody, and truthful liveness has nothing to wake for. Plans, not
            # this source, decides which task takes the executor's slot.
            public.reconcile_plan(
                public.node_row(db, watch.plan_node_id)["plan_id"],
                actor_id=None,
                provenance="continuation.t2_watch_expired",
                db=db,
            )
        if watch.owner_thread is not None:
            plan_liveness.on_turn_complete(
                watch.owner_thread, db=db
            )
        return watches.expiry_event(expired)
