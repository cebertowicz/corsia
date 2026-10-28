"""Writer-side durable material deltas for structured plan mutations."""

from __future__ import annotations

import plans.aggregates as aggregates
import plans.node_states as node_states
import plans.node_waits as node_waits
import plans.service as service


def _blocked(connection, node_ids) -> dict[str, bool]:
    """Each node's dependency-derived blocked truth, in one read."""

    ids = list(dict.fromkeys(node_ids))
    unmet = node_waits.unmet_prerequisites(connection, ids, compact=True)
    states = {
        str(row["id"]): row["state"]
        for row in connection.execute(
            "SELECT id,state FROM plan_nodes WHERE id IN (SELECT value FROM json_each(?))",
            (node_waits.encoded_ids(ids),),
        )
    }
    return {
        node_id: states.get(node_id) in node_waits.WAITABLE_STATES and bool(unmet.get(node_id))
        for node_id in ids
    }


def blocked_snapshot(connection, node_ids) -> dict[str, bool]:
    """Capture dependency-derived blocked truth before a writer mutation.

    Covers each node's waiting descendants too: an edge on a phase holds the
    work beneath it (``node_waits.unmet_prerequisites``), so their history
    records the change the same as the phase's own. The walk skips settled
    subtrees through the open-rows index; migration rungs older than the
    ``folded`` column run this writer too and filter by state instead.
    """

    ids = list(dict.fromkeys(str(node_id) for node_id in node_ids))
    open_c = (
        node_states.listed_open_sql("c") if aggregates.ready(connection) else node_states.open_sql("c")
    )
    waiting = ",".join(f"'{state}'" for state in sorted(node_waits.WAITABLE_STATES))
    beneath = connection.execute(
        f"""WITH RECURSIVE down(id,plan_id,state) AS (
                SELECT c.id,c.plan_id,c.state FROM json_each(?) j
                CROSS JOIN plan_nodes p ON p.id=j.value
                CROSS JOIN plan_nodes c ON c.plan_id=p.plan_id AND c.parent_id=p.id
                WHERE {open_c}
                UNION SELECT c.id,c.plan_id,c.state FROM down
                CROSS JOIN plan_nodes c ON c.plan_id=down.plan_id AND c.parent_id=down.id
                WHERE {open_c})
            SELECT id FROM down WHERE state IN ({waiting})""",
        (node_waits.encoded_ids(ids),),
    ).fetchall()
    return _blocked(connection, [*ids, *(str(row["id"]) for row in beneath)])


def transition_affected_nodes(connection, node_id: str) -> tuple[str, ...]:
    """Return the transitioned node and direct dependency consumers it affects."""

    rows = connection.execute(
        """SELECT node_id FROM plan_node_dependencies
           WHERE depends_on_node_id=? ORDER BY node_id""",
        (node_id,),
    ).fetchall()
    return tuple(dict.fromkeys([node_id, *(row["node_id"] for row in rows)]))


def settle_blocked_deltas(
    connection,
    before: dict[str, bool],
    *,
    actor_id: str | None,
    provenance: str,
    causal_event_id: str,
) -> None:
    """Append only actual blocked false/true writer-side deltas."""

    after = _blocked(connection, before)
    for node_id, prior in before.items():
        current = after[node_id]
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
