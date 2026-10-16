"""The plan header's one display, read from the canonical leaf distribution.

Split from ``operator_node_state``, which owns each node's state and the lead
precedence, so the header's own rules have one home.
"""

from __future__ import annotations

from plans import not_moving
from plans.operator_node_state import leading

_FINISHED_DISPLAY = {
    "failed": {"label_key": "finished", "tone_key": "failed"},
    "done": {"label_key": "finished", "tone_key": "complete"},
    "empty": {"label_key": "finished", "tone_key": "closed"},
}


def display_from_facts(
    plan: dict,
    counts: dict[str, int],
    *,
    still: dict | None,
    working: bool,
    needs_user: bool,
) -> dict:
    """Return one four-state overview from canonical compact facts."""
    if still is not None and still["need_you"]:
        needs_user = True
    display: dict[str, object]
    if working and plan["state"] != "paused":
        display = {"label_key": "working", "tone_key": "active"}
    elif needs_user and plan["state"] != "paused":
        display = {"label_key": "needs_you", "tone_key": "human-review"}
    else:
        lead = leading(counts)
        display = (
            dict(_FINISHED_DISPLAY[lead])
            if lead in _FINISHED_DISPLAY
            else {"label_key": "waiting", "tone_key": "waiting"}
        )
    if still is not None:
        display["not_moving"] = still
    return display


def plan_display(
    plan: dict, counts: dict[str, int], nodes: list[dict], users: frozenset[str] = frozenset(),
) -> dict:
    """Adapt full node presentation to the canonical plan-header reducer."""
    leaves = [node for node in nodes if node.get("branch_progress") is None]
    return display_from_facts(
        plan,
        counts,
        still=not_moving.read(plan, counts, nodes, users),
        working=any(
            node["display"]["label_key"] == "working" and node["id"] not in users
            for node in leaves
        ),
        needs_user=any(node.get("requires_user_action", False) for node in leaves),
    )
