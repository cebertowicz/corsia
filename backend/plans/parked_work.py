"""Which of a helper's commitments its work cannot reach now.

A delegated step reads Working only while its helper is on that step. The
helper's running Turn says it is busy; this says whether the step's own rows
are where that work can be. When every open row for the step's commitment is
parked, a busy helper is busy elsewhere: the step still waits on it
(``wait_causes.delegation_cause``) but does not read Working.

Both plan-state reads build the set here from the same canonical facts: the
full projection from admission's executions, the compact Activity read from
its reduced nodes.
"""

from __future__ import annotations


def parked_commitments(rows, conditions: dict, causes: dict) -> frozenset[str]:
    """Commitments a helper holds open rows for, none of which its work can reach now.

    A row is reachable unless its canonical condition waits, blocks or pauses
    it (a row whose own delegated child works it is given as running). A row
    that waits only behind prerequisites in this plan is reachable when one of
    them is: a row sequenced after the helper's running row is that work.
    Only a commitment whose every open leaf is unreachable is parked.
    """
    def reachable(node_id: str, seen: set[str]) -> bool:
        if node_id not in conditions:
            return False
        if conditions[node_id] not in {"waiting", "blocked", "paused"}:
            return True
        if node_id in seen:
            return False
        seen.add(node_id)
        own = causes.get(node_id) or []
        return bool(own) and all(cause.get("reason_key") == "dependency" for cause in own) and any(
            reachable(str(cause["dependency_node_id"]), seen) for cause in own
        )

    parked: set[str] = set()
    moving: set[str] = set()
    for row in rows:
        key = str(row["id"])
        (moving if reachable(key, set()) else parked).add(str(row["commitment_id"]))
    return frozenset(parked - moving)
