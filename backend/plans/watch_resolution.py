"""The event a plan task waited for ends its wait, once it has actually arrived.

A task can wait on something outside the plan: CI on a pull request, a review on
a document. ``arm_watch`` records that wait against the artifact whose events end
it and the exact event that does. This is the other half of that promise.

Two things decide it, and both are the reason this lives apart from arming:

- **Identity.** An artifact is ``(integration, type, external_id)``. Matching on
  the external id alone would let one integration's pull request end another
  integration's wait, across projects.
- **Delivery.** The wait ends only once the event has reached the thread that
  was waiting. Settling earlier would re-admit a task whose owner never received
  what it was waiting for, so the router calls this after delivery succeeds and
  not before.

Only the named event resolves a watch: an unrelated comment on the same pull
request is not the end of a CI wait.
"""

from __future__ import annotations

_ARMED_FOR_EVENT = """
    SELECT id, plan_id, plan_node_id, owner_thread_id
      FROM expected_event_watches
     WHERE artifact_integration=? AND artifact_type=? AND artifact_external_id=?
       AND expected_event=? AND owner_thread_id=? AND status='armed'
     ORDER BY id
"""


def resolve_delivered_event(
    identity, internal_event: str, *, recipient: str, db=None,
) -> list[str]:
    """Resolve the watches this delivered event ends for the thread it reached.

    ``recipient`` is the thread the event was actually delivered to, and only
    that thread's watches resolve. Two threads can hold the same artifact and
    both wait on the same event; a delivery to one of them is not the other's
    payload, and ending its wait would re-admit a task whose owner never
    received what it was waiting for.
    """
    from automation import plan_liveness, watches
    from plans import admission, service, watch_operations

    if identity is None or not internal_event or not recipient:
        return []
    with service._connection(db) as connection:
        rows = connection.execute(
            _ARMED_FOR_EVENT,
            (
                identity.integration, identity.type, identity.external_id,
                internal_event, recipient,
            ),
        ).fetchall()
        if not rows:
            return []
        with service._write(connection):
            # The read above is outside this write, so a concurrent delivery
            # for the same watch may have resolved a row since. Only the rows
            # this write actually flipped get a history event and a reconcile.
            resolved = [
                row for row in rows
                if watches.resolve_watch(row["id"], db=connection)
            ]
            if not resolved:
                return []
            for row in resolved:
                if row["plan_node_id"]:
                    watch_operations.record_resolved(
                        connection, str(row["plan_node_id"]), watch_id=row["id"],
                        expected_event=internal_event, actor_id=None,
                    )
            for plan_id in sorted({row["plan_id"] for row in resolved if row["plan_id"]}):
                admission.reconcile_plan(
                    str(plan_id), actor_id=None,
                    provenance="event.watch_resolved", db=connection,
                )
            # The armed watch had cancelled the owner's plan wake. With the wait
            # over, that thread needs its liveness re-derived or it is left with
            # neither a watch nor a wake.
            for thread_id in sorted(
                {row["owner_thread_id"] for row in resolved if row["owner_thread_id"]}
            ):
                plan_liveness.on_turn_complete(
                    str(thread_id), db=connection,
                )
        return [str(row["id"]) for row in resolved]
