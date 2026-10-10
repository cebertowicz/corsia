"""Writer-side durable material deltas for structured plan mutations."""

from __future__ import annotations

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
