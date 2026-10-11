"""A delegated step takes its executor's slot back when its worker's wait clears.

A delegated step's child wait (``wait_causes.delegation_causes``) is read from
the worker: its running Turn, queued input and plan. None of those is a write
in the delegator's plan, so nothing reconciled the delegator when the wait
cleared, and its next tool call restored the selection through the dispatch
fallback instead (#2168). The worker's side reconciles it now: every
reconciliation of the worker's plan (``admission.reconcile_plan``) and the
worker's Turn end (``delegation_turn_end.on_worker_turn_end``).

Taking the slot back is state, not news. What a parent hears about its worker
is the notification matrix's, written by ``delegation_notice._wake_parent``
alone (debt ledger 1), so ``selection_wake.notify`` asks ``told_elsewhere``
before telling an executor its slot was filled.
"""

from __future__ import annotations

from plans.hierarchy import delegation_edge_sql

#: Provenance of a delegator's selection reconciled from its worker's side.
DELEGATED_WAIT_CHANGED = "delegated_wait_changed"

# The delegator's step for this worker plan, when its child wait may have
# changed: the worker is not running (the wait can clear only then), or the
# step still holds a selection (a running worker makes it invalid).
_DELEGATOR_SQL = f"""
    SELECT p.parent_plan_id FROM plans p
      JOIN plans parent ON parent.id=p.parent_plan_id AND parent.state='active'
      JOIN plan_nodes n ON {delegation_edge_sql('n', 'p')}
     WHERE p.id=? AND n.kind='task'
       AND n.state='active' AND n.archived_at IS NULL
       AND (NOT EXISTS (SELECT 1 FROM active_turns a
                         WHERE a.thread_id=p.owner_id AND a.status='running')
            OR EXISTS (SELECT 1 FROM plan_node_attempts t
                        WHERE t.node_id=n.id AND t.released_at IS NULL))
     LIMIT 1
"""


def reconcile_delegator(plan_id: str, *, db):
    """Reconcile the plan that delegated to this worker plan, if its wait may have changed.

    Returns the delegator's ``admission.Reconciled``, or None when no step
    there waits on this worker. One level only: the delegator's own wait on
    its parent does not change with its worker.
    """
    from plans import admission

    row = db.execute(_DELEGATOR_SQL, (plan_id,)).fetchone()
    if row is None:
        return None
    return admission.reconcile_plan(
        str(row["parent_plan_id"]), actor_id=None, provenance=DELEGATED_WAIT_CHANGED,
        from_worker=True, db=db,
    )


def told_elsewhere(db, executor: str, node, *, from_worker: bool) -> bool:
    """Whether news of this step's selection is the notification matrix's, not a wake's.

    True when the worker's side selected a step still delegated: the worker's
    news is the matrix's to tell. Also true when the parent has that worker's
    news about the step still queued (the worker ended, F3, and the step came
    back to it). Any other selection, such as a third party freeing the
    parent's slot after the news was heard, is news only this wake carries.
    """
    from plans import wake_currency

    if from_worker and node["spawned_thread_id"]:
        return True
    return wake_currency.step_news_queued(db, executor, str(node["id"]))
