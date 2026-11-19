"""The top of a plan in priority order: what its owner sees without reading the plan.

Plan order is priority. An item's place is its ``order_key`` as ``reorder``
takes it: the count of open top-level siblings before it in ``(order_key, id)``
order (``aggregates.open_siblings_sql``), Later items included, since a move
counts them. The list shows the open top-level items above the cut, in that
order, one short line each: the place, the title, and what it is doing. Later
items keep their places but are not listed; the tail line counts both.

Two readers share this one composition: the thin plan status every owner gets
in its digest and checkpoint (``plans.status_context``), and the reply to an
owner's own top-level ``add`` or ``insert``, which also gets a landing line
asking it to place the item. Adds and moves by anyone else reach the owner as
one passive line (``tell_owner_added``, ``tell_owner_moved``) through
``plans.plan_events.append_owner_event``. Every word comes from
``prompts.plan_top``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import plans.aggregates as aggregates
import plans.service as service
from plans.close_next_bounds import bounded, title as bounded_title
from prompts import plan_top as text

#: Rows the list shows: the top ten, one short line each (the person's
#: approved bound for the placing reply, 2026-11-18). The status fit may show
#: fewer when its byte budget is spent; the tail line always counts the rest.
TOP_ROWS = 10
#: A listed title is cut like every plan title (``close_next_bounds.TITLE_MAX_BYTES``).
#: What a waiting, blocked or paused row waits on, cut short.
WAIT_MAX_BYTES = 40
#: Conditions whose first cause is worth its bytes on a listed row.
_REASONED = frozenset({"waiting", "blocked", "paused"})


@dataclass(frozen=True)
class Top:
    """The listed lines above the cut, and the counts the tail line names."""

    lines: list[str]
    above_cut: int
    later: int

    def shown(self, rows: int | None = None) -> dict[str, Any]:
        """The list as an agent reads it, with at most ``rows`` lines."""
        lines = self.lines if rows is None else self.lines[:rows]
        top: dict[str, Any] = {"order": text.TOP_ORDER, "items": lines}
        more = text.top_more(self.above_cut - len(lines), self.later)
        if more:
            top["more"] = more
        return top


def open_roots(db, plan_id: str) -> list:
    """The plan's open top-level rows in plan order; the index is the place ``reorder`` takes."""
    return db.execute(
        "SELECT id, title, later FROM plan_nodes AS n "
        f"WHERE n.plan_id=? AND n.parent_id IS NULL AND {aggregates.open_siblings_sql(db, 'n')} "
        "ORDER BY n.order_key, n.id",
        (plan_id,),
    ).fetchall()


def compose(db, plan_id: str, facts: dict, *, include: str | None = None) -> Top:
    """Compose the list from the plan's open roots and one status read's facts.

    ``include`` names a root whose line is listed even below the first
    ``TOP_ROWS``: an add's own item, so the agent sees where it landed.
    """
    from plans import status

    by_id = {node["id"]: node for node in facts["nodes"]}
    lines: list[str] = []
    above = later = 0
    for place, root in enumerate(open_roots(db, plan_id)):
        node_id = str(root["id"])
        if root["later"]:
            later += 1
            continue
        above += 1
        if len(lines) < TOP_ROWS or node_id == include:
            lines.append(_line(place, str(root["title"]), by_id.get(node_id), status))
    return Top(lines, above, later)


def _line(place: int, title: str, node: dict | None, status) -> str:
    """One listed row: place, title, and what it is doing."""
    words = [f"{place} {bounded_title(title)}"]
    doing = _doing(node, status)
    if doing:
        words.append(doing)
    return " · ".join(words)


def _doing(node: dict | None, status) -> str | None:
    """The row's shown condition, with what it waits on when that names something."""
    if node is None:
        return None
    if node.get("requires_user_action"):
        return text.TOP_NEEDS_USER
    condition = status._condition(node)
    reason = _waits_on(node, status) if condition in _REASONED else None
    if not reason:
        return condition
    return f"{condition}: {bounded(str(reason), WAIT_MAX_BYTES)}"


def _waits_on(node: dict, status) -> str | None:
    """What the row waits on, in the words its waiter wrote when there are any.

    A dated wait's shown reason leads with its date ("Until <time> · ..."),
    which would spend the row's few bytes on the date, so the waiter's own
    reason or blocker text is used in its place.
    """
    reason = status._reason(node)
    causes = node["execution"].get("causes") or []
    if causes and reason == causes[0].get("reason"):
        own = causes[0].get("scheduled_reason") or causes[0].get("blocked_reason")
        if own:
            return own
    return reason


def place_of(db, plan_id: str, node_id: str) -> tuple[int, int] | None:
    """A root's place and how many open top-level items there are, or None."""
    roots = [str(row["id"]) for row in open_roots(db, plan_id)]
    if node_id not in roots:
        return None
    return roots.index(node_id), len(roots)


def placing_reply(db, plan_row, node_id: str, *, actor, inserted: bool) -> dict[str, Any] | None:
    """The list and landing line an owner's own top-level add or insert gets, or None.

    None for a nested add, an add by anyone but the plan's owner (they are told
    through ``tell_owner_added``), and a live worker's plan: a worker has one job
    and its parent orders it.
    """
    from plans import status

    node = service._node_row(db, node_id)
    owner = str(plan_row["owner_id"])
    if (
        node["parent_id"] is not None
        or plan_row["owner_kind"] != "thread"
        or getattr(actor, "thread_id", None) != owner
        or _worker_plan(db, plan_row)
    ):
        return None
    where = place_of(db, str(plan_row["id"]), node_id)
    if where is None:
        return None
    facts = status._facts(db, plan_row, actor, service._now(), costs=False)
    top = compose(db, str(plan_row["id"]), facts, include=node_id)
    place, total = where
    landing = text.top_landing(
        place, node_id, int(node["revision"]), last=not inserted and place == total - 1,
    )
    return {"top_of_plan": top.shown(), "landing": landing}


def _worker_plan(db, plan_row) -> bool:
    """Whether this plan belongs to a live delegated worker."""
    if plan_row["parent_plan_id"] is None:
        return False
    from plans import projections

    return bool(projections.live_delegation(
        db, str(plan_row["owner_id"]), str(plan_row["parent_plan_id"]),
    ))


def _who(db, actor_id: str | None) -> str:
    """Name the mover or adder as the owner reads it."""
    if actor_id is None or actor_id.startswith("user:"):
        return text.TOP_THE_USER
    row = db.execute("SELECT title FROM threads WHERE id=?", (actor_id,)).fetchone()
    title = row["title"] if row is not None and row["title"] else None
    return f"{title} ({actor_id})" if title else actor_id


def _foreign(plan_row, actor_id: str | None) -> bool:
    """Whether someone other than the plan's own thread made this change."""
    return (
        plan_row["owner_kind"] == "thread"
        and actor_id is not None
        and actor_id != str(plan_row["owner_id"])
    )


def tell_owner_added(db, node_id: str, *, actor_id: str | None) -> None:
    """Tell a standing owner, in its digest, that someone else added a top-level item."""
    from plans.plan_events import append_owner_event

    node = service._node_row(db, node_id)
    plan_row = service._plan_row(db, node["plan_id"])
    if node["parent_id"] is not None or not _foreign(plan_row, actor_id) or _worker_plan(db, plan_row):
        return
    where = place_of(db, str(plan_row["id"]), node_id)
    if where is None:
        return
    event = _last_event(db, node_id, "node_created")
    place, _total = where
    append_owner_event(
        db, plan_id=str(plan_row["id"]), event_id=str(event["id"]), actor_id=actor_id,
        event_type=text.TOP_ADDED_KIND,
        summary=text.top_added_by(
            _who(db, actor_id), str(node["title"]), node_id, int(node["revision"]), place,
        ),
        details={"node_id": node_id, "place": place, "chip": None},
        created_at=str(event["created_at"]),
    )


def last_move_id(db, node_id: str) -> str | None:
    """The node's latest move event, read before a reorder so a no-op move tells nobody."""
    event = _last_event(db, node_id, "node_reordered")
    return None if event is None else str(event["id"])


def tell_owner_moved(db, node_id: str, *, actor_id: str | None, before: str | None) -> None:
    """Tell a plan's owner, in its digest, that someone else moved one of its items.

    ``before`` is ``last_move_id`` from before the reorder: a reorder to the
    place an item already holds records no move, and so tells nobody.
    """
    from plans.plan_events import append_owner_event

    node = service._node_row(db, node_id)
    plan_row = service._plan_row(db, node["plan_id"])
    if not _foreign(plan_row, actor_id):
        return
    event = _last_event(db, node_id, "node_reordered")
    if event is None or str(event["id"]) == before:
        return
    moved = json.loads(event["payload_json"] or "{}")
    append_owner_event(
        db, plan_id=str(plan_row["id"]), event_id=str(event["id"]), actor_id=actor_id,
        event_type=text.TOP_MOVED_KIND,
        summary=text.top_moved_by(
            _who(db, actor_id), str(node["title"]), node_id, moved.get("from"), moved.get("to"),
            nested=node["parent_id"] is not None,
        ),
        details={"node_id": node_id, "from": moved.get("from"), "to": moved.get("to"), "chip": None},
        created_at=str(event["created_at"]),
    )


def _last_event(db, node_id: str, event: str):
    return db.execute(
        "SELECT id, created_at, payload_json FROM plan_node_events "
        "WHERE node_id=? AND event=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (node_id, event),
    ).fetchone()
