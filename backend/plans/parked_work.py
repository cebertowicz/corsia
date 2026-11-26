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

from plans import operator_scheduled


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


def set_aside_reason(causes_by_leaf) -> str | None:
    """The shown reason of a helper that set every open step aside, else None.

    Only the full read names a set-aside (``scheduled_reason``); the compact
    read's causes never do, so it answers None and keeps the generic wait.
    When several steps are set aside, the one ending first is when the helper
    comes back.
    """
    leaves = [list(causes) for causes in causes_by_leaf]
    if not leaves or not all(operator_scheduled.only_set_aside(causes) for causes in leaves):
        return None
    dated = [
        (moment, cause) for causes in leaves for cause in causes
        if (moment := operator_scheduled.instant(cause.get("ends_at"))) is not None
    ]
    if not dated:
        return None
    return min(dated, key=lambda item: item[0])[1].get("reason") or None
