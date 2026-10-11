"""Delegation reads on the attachment epoch: open work plus what settled since (#149 S2d).

A delegated child plan's return is judged against the attachment that handed the step
over, so the harvest and the worker's retirement read the child's *open* rows (the
open index) and the steps that changed since the current attachment
(``idx_plan_nodes_recent``), never every leaf the child plan ever held. A step settled
before the attachment and untouched since is not part of any answer these reads give
(``returned_since`` asks for a transition after it), so leaving it unread changes no
result. ``since`` is the attachment event's time; an event of the epoch is never older.

Before the aggregate columns exist (``aggregates.ready``), or with no attachment to
bound the read, every function here reads the whole plan, as before.
"""

from __future__ import annotations

import plans.node_states as node_states
from plans import aggregates, open_tree

_OPEN_STATES = ("pending", "active", "review_requested")
_OPEN_MARKS = ",".join(f"'{state}'" for state in _OPEN_STATES)


def attached_since(connection, node_id: str) -> str | None:
    """The time of the node's current (latest) attachment event, or None."""
    from plans.delegation import _latest_attachment

    event = _latest_attachment(node_id, connection)
    return None if event is None else str(event["created_at"])


def open_row_sql(connection, alias: str) -> str:
    """Narrow a read of open-state rows to the open index; a no-op before the aggregate columns.

    A row in an open state is never folded unless it is a never-started container
    whose steps have all settled, which no caller of this lists.
    """
    return f"{alias}.folded=0" if aggregates.ready(connection) else "1"


def _flat(rows) -> list:
    return sorted(rows, key=lambda row: (row["order_key"], str(row["id"])))


def open_leaves(connection, plan_id: str) -> list:
    """The plan's leaves with work open in them, in plan order (``node_states.leaves``)."""
    from plans import service

    if not aggregates.ready(connection):
        return [row for row in service._plan_leaf_rows(connection, plan_id) if row["state"] in _OPEN_STATES]
    return _flat(open_tree.leaves(open_tree.load(connection, plan_id)))


def _has_leaves(connection, plan_id: str) -> bool:
    """Whether the plan holds a leaf: some node is in the work (the finite tree ends in one)."""
    row = connection.execute("SELECT top_in_work FROM plans WHERE id=?", (plan_id,)).fetchone()
    return bool(row["top_in_work"])


def _closed_steps(connection, plan_id: str, since: str | None) -> list:
    """Closed steps with no closed step beneath them, changed since ``since`` (all when None).

    What a plan whose remaining work was all closed returns: its closed steps, in
    ``order_key, id`` order. Closed rows rest in the folded index unless work is
    still open beneath them, when the open index holds them.
    """
    recent, params = ("AND n.own_last_changed>=?", (since,)) if since is not None else ("", ())
    rows: dict[str, object] = {}
    for folded in (1, 0):
        for row in connection.execute(
            f"""SELECT n.* FROM plan_nodes n
                WHERE n.plan_id=? AND {node_states.unarchived_sql('n')} AND n.state='closed' AND n.folded={folded} {recent}
                  AND NOT EXISTS (
                    SELECT 1 FROM plan_nodes child
                    WHERE child.parent_id=n.id AND child.state='closed'
                      AND {node_states.unarchived_sql('child')})""",
            (plan_id, *params),
        ):
            rows[str(row["id"])] = row
    return _flat(rows.values())


def _any_closed_step(connection, plan_id: str) -> bool:
    """Whether the plan holds a closed, unarchived node (so it has a closed step to return)."""
    return any((
        connection.execute(
            f"""SELECT 1 FROM plan_nodes n
                WHERE n.plan_id=? AND {node_states.unarchived_sql('n')}
                  AND n.state='closed' AND n.folded=?
                LIMIT 1""",
            (plan_id, folded),
        ).fetchone() is not None
        for folded in (1, 0)
    ))


def return_rows(connection, plan_id: str, since: str | None = None) -> list:
    """The steps a delegated plan's return is made of, in plan order.

    A worker's return is its leaves. Closed steps are out of the work
    (``node_states.work_ids``), so a plan whose remaining work was all closed
    has no leaf left; its closed steps are the return then, and the parent
    reads each as ``closed``. A plan that never held a step has neither, and
    returned nothing. Given ``since`` (the attachment epoch) the read lists the
    open leaves and the leaves changed since it, never the older settled ones.
    """
    from plans import service

    if since is None or not aggregates.ready(connection):
        leaves = service._plan_leaf_rows(connection, plan_id)
        return leaves if leaves else _closed_steps(connection, plan_id, None)
    if _has_leaves(connection, plan_id):
        return _flat(open_tree.leaves(open_tree.load(connection, plan_id, recent_since=since)))
    return _closed_steps(connection, plan_id, since)


def delegated_work_returned(connection, plan_id: str) -> bool:
    """Return whether a delegated plan has returned: steps exist, none still runs.

    A worker returns by putting its last step up for review, failing it, or
    closing it. Every unarchived leaf is then settled or awaiting the parent's
    verdict; a plan whose remaining work was all closed has only closed steps
    left, and that is its return too. A plan that never held a step has
    returned nothing, and a step awaiting its own owner's verdict still runs.
    So does a step that waits on an answer from the user or the parent
    (``delegation_harvest.waiting_on_answer``): the worker acts on the answer
    before the parent judges the work.

    Only the open leaves can run or await a verdict, so they decide it; settled
    history only says whether the plan holds a step at all.
    """
    from plans import delegation_harvest

    if not aggregates.ready(connection):
        steps = return_rows(connection, plan_id)
        open_steps = [row for row in steps if row["state"] in _OPEN_STATES]
        has_steps = bool(steps)
    else:
        open_steps = open_leaves(connection, plan_id)
        has_steps = bool(open_steps) or _has_leaves(connection, plan_id) or _any_closed_step(connection, plan_id)
    return has_steps and not delegation_harvest.waiting_on_answer(connection, plan_id) and all(
        row["state"] not in ("pending", "active")
        and not delegation_harvest.awaiting_owner_verdict(connection, row)
        for row in open_steps
    )


def open_delegated_node_ids(connection, plan_id: str, *, limit: int = -1) -> tuple[str, ...]:
    """Return up to ``limit`` open step ids with nothing open beneath them.

    The one answer to "does the worker still have work open": an open leaf, or
    a started step (not ``node_states.is_container``) whose own steps have all
    settled, which would otherwise read in progress under finished work. A
    never-started container reads as its steps. Settling one can open its
    parent, so a caller that settles them asks again until none remain.

    A refusal that says only *that* a child plan is unfinished leaves the owner
    hunting for the rows; naming them is what makes it actionable. ``limit``
    (all by default) keeps the mere-existence caller below at ``LIMIT 1``.

    Such a step is never folded, so it sits in the open index; an open-state
    child is either an open row or a never-started container whose steps have
    all settled (the only open-state row that folds).
    """
    if aggregates.ready(connection):
        return _open_ids_indexed(connection, plan_id, limit)
    return tuple(
        str(row["id"])
        for row in connection.execute(
            f"""SELECT n.id FROM plan_nodes n
               WHERE n.plan_id=? AND {node_states.in_work_sql('n')}
                 AND n.state IN ({_OPEN_MARKS})
                 AND NOT EXISTS (SELECT 1 FROM plan_nodes c
                      WHERE c.parent_id=n.id AND {node_states.in_work_sql('c')}
                        AND c.state IN ({_OPEN_MARKS}))
                 AND NOT {node_states.is_container_sql('n')}
               ORDER BY n.order_key,n.created_at
               LIMIT ?""",
            (plan_id, limit),
        )
    )


def _open_ids_indexed(connection, plan_id: str, limit: int) -> tuple[str, ...]:
    in_work = node_states.in_work_sql("c")
    return tuple(
        str(row["id"])
        for row in connection.execute(
            f"""SELECT n.id FROM plan_nodes n
               WHERE n.plan_id=? AND {node_states.in_work_sql('n')} AND n.folded=0
                 AND n.state IN ({_OPEN_MARKS})
                 AND NOT (n.child_in_work>0 AND n.state='pending' AND n.spawned_thread_id IS NULL)
                 AND NOT EXISTS (SELECT 1 FROM plan_nodes c
                      WHERE c.plan_id=n.plan_id AND c.parent_id=n.id AND c.folded=0
                        AND {in_work} AND c.state IN ({_OPEN_MARKS}))
                 AND NOT EXISTS (SELECT 1 FROM plan_nodes c
                      WHERE c.plan_id=n.plan_id AND c.parent_id=n.id AND c.folded=1
                        AND {in_work} AND c.state='pending')
               ORDER BY n.order_key,n.created_at
               LIMIT ?""",
            (plan_id, limit),
        )
    )


def open_delegated_work(connection, plan_id: str) -> bool:
    """Return whether a plan still holds open work (``open_delegated_node_ids``)."""

    return bool(open_delegated_node_ids(connection, plan_id, limit=1))
