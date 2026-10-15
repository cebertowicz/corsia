"""What a presented node's own agent set aside, read by the one branch roll-up.

``operator_nodes._branch_summary`` rolls these facts up the tree with the leaf
buckets; nothing else walks the tree for them.
"""

from __future__ import annotations

from datetime import datetime


def _instant(value: str | None) -> datetime | None:
    """A stored deadline as an aware instant; one without a zone has no knowable end."""
    try:
        moment = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    return moment if moment is not None and moment.tzinfo is not None else None


def needs_no_one(node: dict) -> bool:
    """Whether nothing on a presented node itself asks a person: no user action, hold, assignment or raised ask."""
    return not (
        node["requires_user_action"] or node["held_by"] == "user" or node["assignee"] == "user"
        or any(
            link["role"] == "attention" and link.get("attention_state") == "raised"
            for link in node["artifacts"]
        )
    )


def is_set_aside(cause: dict) -> bool:
    """Whether a waiting cause records that the executor scheduled this task itself."""
    return cause.get("kind") == "waiting" and cause.get("scheduled_reason") is not None


def own_wait(node: dict) -> tuple[bool, datetime | None]:
    """The node's own timed wait: whether it has one, and its end when its agent set it aside.

    A wait ending by itself is *scheduled* when its agent armed it with a reason
    (``plans.scheduled_wait``); any other deadline watch is an ordinary wait.
    """
    execution = node["execution"]
    if execution["condition"] != "waiting":
        return False, None
    cause = next(
        (item for item in execution["causes"] if item.get("kind") == "waiting" and item.get("ends_at")),
        None,
    )
    if cause is None:
        return False, None
    return True, _instant(cause["ends_at"]) if is_set_aside(cause) else None


def scheduled_leaf_end(node: dict) -> datetime | None:
    """The one home of "quietly scheduled" for open work with no open work beneath it.

    The end of a wait its own agent set aside until a date, on a node needing no
    one. A parent whose children are all settled is such a leaf: its own wait is
    its only open work.
    """
    return own_wait(node)[1] if needs_no_one(node) else None
