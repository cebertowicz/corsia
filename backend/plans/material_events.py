"""Writer-side durable material deltas for structured plan mutations."""

from __future__ import annotations

import plans.node_states as node_states
import plans.node_waits as node_waits
import plans.service as service


def blocked_snapshot(connection, node_ids) -> dict[str, bool]:
    """Capture dependency-derived blocked truth before a writer mutation."""

    snapshot = {}
    for node_id in dict.fromkeys(node_ids):
        row = service._node_row(connection, node_id)
        snapshot[node_id] = node_waits.waits_on_dependency(
            connection, node_id, row["state"]
        )
    return snapshot


def transition_affected_nodes(connection, node_id: str) -> tuple[str, ...]:
    """Return the transitioned node and direct dependency consumers it affects."""

    rows = connection.execute(
        """SELECT node_id FROM plan_node_dependencies
           WHERE depends_on_node_id=? ORDER BY node_id""",
        (node_id,),
    ).fetchall()
    return tuple(dict.fromkeys([node_id, *(row["node_id"] for row in rows)]))


def transition_before(connection, node, *, ends: bool):
    """A transition's snapshot: its own and its dependents' blocked truth, and, when it
    ends ``node``'s work, the phases above it that work waits on (``phases_waited_on``)."""
    affected = transition_affected_nodes(connection, node["id"])
    phases = phases_waited_on(connection, node["parent_id"], skip=affected) if ends else ({}, {})
    return blocked_snapshot(connection, affected), phases


def settle_transition(connection, before, *, actor_id: str | None, provenance: str, causal_event_id: str) -> None:
    """The deltas of ``transition_before``'s snapshot, once the transition is written."""
    blocked, phases = before
    settle_blocked_deltas(
        connection, blocked, actor_id=actor_id, provenance=provenance, causal_event_id=causal_event_id,
    )
    settle_phases_waited_on(
        connection, phases, actor_id=actor_id, provenance=provenance, causal_event_id=causal_event_id,
    )


def phases_waited_on(connection, parent_id: str | None, *, skip=()) -> tuple[dict[str, str], dict[str, bool]]:
    """Each pending phase at or above ``parent_id`` that work waits on, with the state its waiters
    read (``node_waits.prerequisite_state_sql``), and those waiters' blocked truth, before a write (#3145).

    A phase's own state never moves when its work ends, so the write that ends it --
    its last open work settling or leaving -- takes this snapshot and hands it to
    ``settle_phases_waited_on``. ``skip`` holds dependents the caller already snapshots.
    """
    if parent_id is None:
        return {}, {}
    states = {
        str(row["id"]): str(row["state"]) for row in connection.execute(
            f"""WITH RECURSIVE up(id) AS (
                    SELECT ? UNION ALL
                    SELECT a.parent_id FROM up CROSS JOIN plan_nodes a ON a.id=up.id WHERE a.parent_id IS NOT NULL
                )
                SELECT p.id,{node_waits.prerequisite_state_sql('p')} AS state
                FROM up CROSS JOIN plan_nodes p ON p.id=up.id
                WHERE p.kind='phase' AND p.state='pending' AND {node_states.in_work_sql('p')}
                  AND EXISTS (SELECT 1 FROM plan_node_dependencies d WHERE d.depends_on_node_id=p.id)""",
            (parent_id,),
        )
    }
    waiting = [
        str(row["node_id"]) for row in connection.execute(
            "SELECT DISTINCT node_id FROM plan_node_dependencies WHERE depends_on_node_id IN (SELECT value FROM json_each(?))"
            " ORDER BY node_id",
            (node_waits.encoded_ids(states),),
        )
    ]
    return states, blocked_snapshot(connection, [node_id for node_id in waiting if node_id not in set(skip)])


def settle_phases_waited_on(
    connection, before: tuple[dict[str, str], dict[str, bool]], *,
    actor_id: str | None, provenance: str, causal_event_id: str,
) -> None:
    """After the write, release and tell what waits on each phase whose work just ended.

    The same two effects a prerequisite's own transition has: the blocked deltas on its
    dependents, then ``cross_plan.prerequisite_settled`` for work in other plans.
    """
    from plans.cross_plan import prerequisite_settled

    states, blocked = before
    settle_blocked_deltas(
        connection, blocked, actor_id=actor_id, provenance=provenance, causal_event_id=causal_event_id,
    )
    for phase_id, prior in states.items():
        state = connection.execute(
            f"SELECT {node_waits.prerequisite_state_sql('p')} AS state FROM plan_nodes p WHERE p.id=?", (phase_id,),
        ).fetchone()["state"]
        if state != prior and state in service._TERMINAL_STATES:
            prerequisite_settled(connection, phase_id, actor_id=actor_id, provenance=provenance)


def settle_blocked_deltas(
    connection,
    before: dict[str, bool],
    *,
    actor_id: str | None,
    provenance: str,
    causal_event_id: str,
) -> None:
    """Append only actual blocked false/true writer-side deltas."""

    for node_id, prior in before.items():
        row = service._node_row(connection, node_id)
        current = node_waits.waits_on_dependency(connection, node_id, row["state"])
        if current == prior:
            continue
        service._event(
            connection,
            node_id,
            "blocked_raised" if current else "blocked_cleared",
            actor_id=actor_id,
            provenance=provenance,
            causal_event_id=causal_event_id,
            payload={"blocked": current},
        )
