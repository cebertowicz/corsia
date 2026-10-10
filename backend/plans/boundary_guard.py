"""Whether the generic plan boundary may drive one node: one non-raising guard.

The mutation boundary refuses a node-targeted operation on a node outside the
requested plan, on an archived node (the archive itself excepted), and on a
node whose settlement a lane owns. A closing or parking operation's named next
task is judged by the same guard plus the rule that binds it to the executor
whose slot the operation freed; there a refusal is reported in the result
instead of raised.
Both ask this one function, so what the boundary refuses and what a named start
reports cannot drift apart. It reads and classifies only; each caller decides
what a refusal means for it.
"""

from __future__ import annotations

from dataclasses import dataclass

import plans.admission_projection as admission_projection
import plans.node_settlement as node_settlement

OUTSIDE_PLAN = "outside_plan"
ARCHIVED = "archived"
LANE_OWNED = "lane_owned"
OTHER_EXECUTOR = "other_executor"


@dataclass(frozen=True)
class DriveRefusal:
    """Why the generic boundary may not drive a node; ``lane`` names an owning lane."""

    code: str
    lane: str = ""


def drive_refusal(
    db, node, *, plan, operation: str, executor: str | None = None,
    by_user: bool = False,
) -> DriveRefusal | None:
    """Return the first reason the generic boundary may not drive this node, or None.

    Checked in order: the node belongs to ``plan``; it is not archived, unless
    ``operation`` is the archive itself; no lane owns its settlement for
    ``operation``; and, when ``executor`` is given, that executor runs it.
    Thread plans resolve every task to their owner; a scope plan's legacy rows
    can still name different executors. ``by_user`` marks the user's own
    steering, which may drop a question put to the user
    (``node_settlement.lane_refusing``).
    """
    if node["plan_id"] != plan["id"]:
        return DriveRefusal(OUTSIDE_PLAN)
    if operation != "archive_node" and node["archived_at"] is not None:
        return DriveRefusal(ARCHIVED)
    lane = node_settlement.lane_refusing(db, node["id"], operation, by_user=by_user)
    if lane is not None:
        return DriveRefusal(LANE_OWNED, lane)
    if executor is not None and admission_projection.executor_of(node, plan) != executor:
        return DriveRefusal(OTHER_EXECUTOR)
    return None
