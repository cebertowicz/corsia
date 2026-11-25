"""Words for the top of a plan (``plans.plan_top``): the list every owner sees,
the landing line on an owner's own add, and the digest lines for an add or move
by someone else."""

from __future__ import annotations

from collections.abc import Sequence

from prompts.plan_tools import in_later

#: What each listed number means.
TOP_ORDER = (
    "Plan order is priority. Each number is order_key: the item's place among "
    "your plan's top-level items, 0 at the top."
)
#: A listed row that waits on the user.
TOP_NEEDS_USER = "needs the user"
#: How the mover or adder is named when it is the user.
TOP_THE_USER = "The user"
_TOP_MORE = "{more} more open below these"
#: An owner's own add or insert: where it landed, and the call that places it.
_TOP_LANDED_LAST = "Added at {place}, the bottom of your plan."
_TOP_LANDED_AT = "Added at {place}."
_TOP_PLACE = (
    " Leave it there, or move it: reorder node_id={node} expected_revision={revision} "
    "order_key=<the number of the item it goes above>."
)
#: The add went past the cap on unstarted items (``plans.plan_cap``) and is in Later.
_TOP_LANDED_LATER = (
    " It went to Later: more than {cap} items were waiting to start. To work it sooner, "
    "pull it back: edit node_id={node} expected_revision={revision} later=false, then reorder it."
)
#: The add went past the cap and lower items made room.
_TOP_MOVED_LATER = " To keep {cap} items waiting to start, this moved to Later: {titles}."
#: Digest change kinds for an add or move by someone other than the owner.
TOP_ADDED_KIND = "plan.added_by_other"
TOP_MOVED_KIND = "plan.moved_by_other"
_TOP_ADDED_BY = (
    "{who} added '{title}' ({node}) to your plan, at {place}. Place it "
    "against the rest of your plan: leave it there, or reorder node_id={node} "
    "expected_revision={revision} order_key=<the number of the item it goes above>."
)
_TOP_MOVED_BY = (
    "{who} moved '{title}' ({node}) from {old} to {new} in your plan. Plan order is "
    "priority: work it in its new place."
)
_TOP_MOVED_BY_NESTED = (
    "{who} moved '{title}' ({node}) from {old} to {new} among its sibling steps. "
    "Plan order is priority: work it in its new place."
)


def top_more(more: int, later: int) -> str:
    """The list's tail: open items it leaves out, then the Later count."""
    tail = _TOP_MORE.format(more=more) if more > 0 else ""
    in_tail = in_later(later)
    if not tail:
        return in_tail.removeprefix(" · ")
    return tail + in_tail


def top_landing(
    place: int, node: str, revision: int, *, last: bool,
    in_later: bool = False, moved: Sequence[str] = (), cap: int = 0,
) -> str:
    """The landing line an owner's own top-level add or insert gets."""
    landed = (_TOP_LANDED_LAST if last else _TOP_LANDED_AT).format(place=place)
    if in_later:
        return landed + _TOP_LANDED_LATER.format(cap=cap, node=node, revision=revision)
    line = landed + _TOP_PLACE.format(node=node, revision=revision)
    if moved:
        line += _TOP_MOVED_LATER.format(cap=cap, titles="; ".join(f"'{title}'" for title in moved))
    return line


def top_added_by(
    who: str, title: str, node: str, revision: int, place: int,
) -> str:
    """The digest line a standing owner gets when someone else adds to its plan."""
    return _TOP_ADDED_BY.format(
        who=who, title=title, node=node, revision=revision, place=place,
    )


def top_moved_by(
    who: str, title: str, node: str, old: int | None, new: int | None, *, nested: bool,
) -> str:
    """The digest line an owner gets when someone else moves one of its items."""
    line = _TOP_MOVED_BY_NESTED if nested else _TOP_MOVED_BY
    return line.format(who=who, title=title, node=node, old=old, new=new)
