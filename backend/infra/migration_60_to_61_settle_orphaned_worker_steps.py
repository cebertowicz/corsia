"""Schema 60 -> 61: a finished delegated step's worker keeps no open step.

A parent's verdict on a delegated step now settles its worker's steps in the
same write (``plans.review_verification._settle_delegated_work`` and
``plans.worker_retirement``): acceptance accepts the steps the worker returned
and stands the rest down, a failure fails them, a close closes them. Before
that, the verdict left them open, so a worker step sat in progress or up for
review under a parent step already finished. This applies the verdict each
such parent has, through those same writers.

What moves: the open steps of the worker plan an unarchived, finished
delegated step shows (``plans.hierarchy.shown_worker_plan``), on an unarchived
plan, when no other open step holds the same worker (a reused worker still
working for another step is left alone). Accepted: the steps the harvest
returned are accepted with this step's verification (one the runtime would
refuse, lacking evidence, is not), the rest close as stood down. Failed: they fail. Closed: they close with the delegating step. A
started step whose own steps have all settled is open work too
(``plans.projections.open_delegated_node_ids``); a never-started one reads as its
steps and is left. A step that delegated further settles its own worker the
same way. Any selection a
settled step still held is released, so no executor slot stays taken.

Worker threads are not retired here: the runtime retires a worker when its
parent's verdict lands, which already happened. Every node event is written
under this step's provenance, so no owner or parent is told
(``plans.plan_events.from_schema_step``). Rerunning finds nothing left open.

Data only; runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import sqlite3

_PROVENANCE = "migration.60_to_61"
_RELEASE_REASON = "settled_by_schema_61"
_OPEN = ("pending", "active", "review_requested")

_FINISHED_DELEGATIONS_SQL = """
    SELECT n.* FROM plan_nodes n
      JOIN plans p ON p.id=n.plan_id AND p.state<>'archived'
     WHERE n.spawned_thread_id IS NOT NULL AND n.archived_at IS NULL
       AND n.state IN ('accepted','accepted_with_followup','failed','closed')
       AND NOT EXISTS (
           SELECT 1 FROM plan_nodes o
            WHERE o.spawned_thread_id=n.spawned_thread_id AND o.id<>n.id
              AND o.archived_at IS NULL
              AND o.state IN ('pending','active','review_requested'))
     ORDER BY n.updated_at, n.id
"""

_OPEN_SELECTIONS_SQL = """
    SELECT a.node_id FROM plan_node_attempts a
      JOIN plan_nodes n ON n.id=a.node_id
     WHERE a.released_at IS NULL AND n.state IN ('pending','active','review_requested')
"""


def migrate_60_to_61(conn: sqlite3.Connection) -> None:
    """Settle every open worker step its finished parent step already judged."""
    from plans import public

    caller_factory, conn.row_factory = conn.row_factory, sqlite3.Row
    try:
        held = {row["node_id"] for row in conn.execute(_OPEN_SELECTIONS_SQL)}
        for parent in conn.execute(_FINISHED_DELEGATIONS_SQL).fetchall():
            worker_plan = public.shown_worker_plan(conn, parent)
            if worker_plan is None or not public.open_delegated_node_ids(conn, worker_plan["id"]):
                continue
            _apply_verdict(conn, public, parent, worker_plan["id"])
        for node_id in sorted(held):
            state = conn.execute(
                "SELECT state FROM plan_nodes WHERE id=?", (node_id,),
            ).fetchone()["state"]
            if state not in _OPEN:
                public.release_attempt(node_id, _RELEASE_REASON, db=conn)
    finally:
        conn.row_factory = caller_factory


def _apply_verdict(conn, public, parent, worker_plan_id: str) -> None:
    if parent["state"] == "failed":
        public.fail_open_worker_steps(
            conn, worker_plan_id, None, provenance=_PROVENANCE,
        )
        return
    if parent["state"] == "closed":
        public.close_open_worker_steps(
            conn, worker_plan_id, None,
            reason=public.CLOSED_WITH_DELEGATION, provenance=_PROVENANCE,
        )
        return
    for leaf in public.returned_worker_steps(conn, parent):
        # A step the runtime would refuse to accept (no evidence for its
        # current requirements) is not accepted late either: it stands down.
        conn.execute("SAVEPOINT accept_returned")
        try:
            public.transition_node_in(
                conn, leaf["id"], "accepted", actor_id=None, provenance=_PROVENANCE,
                verification_result="passed", verified_by=_verified_by(conn, parent),
            )
        except public.PlanError:
            conn.execute("ROLLBACK TO accept_returned")
        conn.execute("RELEASE accept_returned")
    public.close_open_worker_steps(
        conn, worker_plan_id, None, reason=public.STOOD_DOWN, provenance=_PROVENANCE,
    )


def _verified_by(conn, parent) -> str:
    """The verification the parent's acceptance recorded, which its verdict carries."""
    row = conn.execute(
        """SELECT json_extract(payload_json,'$.verified_by') AS verified_by
             FROM plan_node_events
            WHERE node_id=? AND event='state_transition'
              AND new_state IN ('accepted','accepted_with_followup')
            ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (parent["id"],),
    ).fetchone()
    return (row["verified_by"] if row is not None else None) or _PROVENANCE
