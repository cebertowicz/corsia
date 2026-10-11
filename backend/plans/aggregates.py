"""Stored subtree aggregates of plan nodes, and the one hook that keeps them exact (#731).

Every node stores its own contribution and the sums over its descendants; the plan
stores the same sums for its top level. ``apply`` is called by the two writers of an
aggregate input (``service.touch_node`` and the creation path) and re-derives what a
row change moves, one statement per level up to the root. ``plans.aggregates_rebuild``
computes the same figures from scratch (the migration backfill and fixtures), and
``plans.aggregates_recount`` is the independent check both must equal.

Two vectors propagate (design section 4, item 3):

* the fold vector, ``open``, always propagates, because folding ignores retirement;
* the work vector, ``done``/``failed``/``closed``/``in_work``, propagates only while
  the node is not retired; a retired node passes up its own contribution alone.

``apply`` writes aggregate columns only, never ``revision`` or ``updated_at``, and
the plan revision trigger does not watch them (``infra.plan_aggregates_schema``).
"""

from __future__ import annotations

import sqlite3
import weakref
from collections.abc import Mapping
from typing import NamedTuple

import plans.node_states as node_states
from plans import writer_context

#: ``done``, ``failed``, ``closed``, ``in_work``.
Work = tuple[int, ...]
#: Folded rows at one level by tone: ``done``, ``failed``, ``closed``.
Folded = tuple[int, ...]

NO_WORK: Work = (0, 0, 0, 0)
NO_FOLDED: Folded = (0, 0, 0)
WORK_NAMES = ("done", "failed", "closed", "in_work")
TONES = ("done", "failed", "closed")

#: ``touch_node`` columns that are aggregate inputs (with row insert, delete,
#: ``parent_id`` and ``plan_id``, which no runtime code rewrites).
INPUT_COLUMNS = frozenset({"state", "archived_at", "spawned_thread_id", "sent_back"})

_CHAIN_SQL = """
WITH RECURSIVE chain(id, depth) AS (
    SELECT ?, 0
    UNION ALL
    SELECT n.parent_id, chain.depth + 1 FROM plan_nodes n JOIN chain ON n.id = chain.id
    WHERE n.parent_id IS NOT NULL
)
SELECT n.* FROM chain JOIN plan_nodes n ON n.id = chain.id ORDER BY chain.depth
"""

#: Aggregate columns ``apply`` and ``rebuild`` write for every level they visit.
_NODE_WRITES = (
    "child_in_work", "own_in_work", "listed", "own_last_changed",
    "sub_open", "sub_done", "sub_failed", "sub_closed", "sub_in_work",
    "prop_open", "prop_done", "prop_failed", "prop_closed", "prop_in_work",
    "folded", "folded_at", "fold_bucket",
    "child_folded_done", "child_folded_failed", "child_folded_closed",
)
_NODE_UPDATE = (
    "UPDATE plan_nodes SET " + ", ".join(f"{name}=:{name}" for name in _NODE_WRITES) + " WHERE id=:id"
)
_PLAN_COLUMNS = (
    "top_open", "top_done", "top_failed", "top_closed", "top_in_work", "top_roots",
    "top_folded_done", "top_folded_failed", "top_folded_closed",
)


class Own(NamedTuple):
    """One node's own contribution (the design's ``contribution`` output)."""

    open: int
    folded: bool
    container: bool
    work: Work


def contribution(row: Mapping, child_in_work: int) -> Own:
    """Restate today's classification for one row; the only place it lives.

    An archived row contributes zero to every field. ``open`` is the row-local
    fold answer; the all-descendants rule joins it with ``sub_open``.
    """
    if row["archived_at"] is not None:
        return Own(0, False, False, NO_WORK)
    state = row["state"]
    container = node_states.is_container(row, child_in_work > 0)
    folded = node_states.folded(state, None, container=container)
    retired = state == node_states.CLOSED
    leaf = child_in_work == 0 and not retired
    return Own(
        open=int(not folded),
        folded=folded,
        container=container,
        work=(
            int(leaf and state in node_states._ACCEPTED_STATES),
            int(leaf and state == "failed"),
            int(retired and not row["sent_back"]),
            int(not retired),
        ),
    )


def add(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(a + b for a, b in zip(left, right))


def sub(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(a - b for a, b in zip(left, right))


def weight(tone: str | None) -> Folded:
    return (int(tone == "done"), int(tone == "failed"), int(tone == "closed"))


def sub_work(node: Mapping) -> Work:
    return (node["sub_done"], node["sub_failed"], node["sub_closed"], node["sub_in_work"])


def prop_work(node: Mapping) -> Work:
    return (node["prop_done"], node["prop_failed"], node["prop_closed"], node["prop_in_work"])


def child_folded(node: Mapping) -> Folded:
    return (node["child_folded_done"], node["child_folded_failed"], node["child_folded_closed"])


def _fold_tone(node: Mapping, own: Own, folded: bool) -> str | None:
    """The tone a folded row shows (``foldLabel``), from its row and stored sums.

    A container reads as its leaves (failed, else done, else closed); any other
    folded row shows its own terminal state, however many children it has. An
    archived row and a sent-back round are never listed.
    """
    if node["archived_at"] is not None or node["sent_back"] or not folded:
        return None
    if own.container:
        return "failed" if node["sub_failed"] else "done" if node["sub_done"] else "closed"
    if node["state"] in node_states._ACCEPTED_STATES:
        return "done"
    return "failed" if node["state"] == "failed" else "closed"


def refresh(node: dict, stamp: str) -> tuple[int, Work, Folded, int]:
    """Recompute one node from its row and stored sums, in place.

    Returns what changed for the parent: the fold-vector delta, the work-vector
    delta, the change in the node's tone and the change in its own ``in_work``.
    The delta is new minus stored, never a recomputation of the old value.
    """
    own = contribution(node, node["child_in_work"])
    child_delta = own.work[3] - node["own_in_work"]
    node["own_in_work"] = own.work[3]
    passed_open = own.open + node["sub_open"]
    passed_work = own.work if node_states.retired(node) else add(own.work, sub_work(node))
    d_open = passed_open - node["prop_open"]
    d_work = sub(passed_work, prop_work(node))
    node["prop_open"] = passed_open
    node["prop_done"], node["prop_failed"], node["prop_closed"], node["prop_in_work"] = passed_work
    folded = own.folded and node["sub_open"] == 0
    if folded and not node["folded"]:
        node["folded_at"] = stamp
    elif not folded:
        node["folded_at"] = None
    node["folded"] = int(folded)
    tone = _fold_tone(node, own, folded)
    d_folded = sub(weight(tone), weight(node["fold_bucket"]))
    node["fold_bucket"] = tone
    return d_open, d_work, d_folded, child_delta


def persist(connection: sqlite3.Connection, nodes: list[dict]) -> None:
    """Write the aggregate columns of the given nodes, one statement each."""
    with writer_context.sanctioned_write():
        for node in nodes:
            connection.execute(_NODE_UPDATE, node)


#: Per connection, the schema version at which the aggregate columns were last seen.
_READY: weakref.WeakKeyDictionary[sqlite3.Connection, int] = weakref.WeakKeyDictionary()


def ready(connection: sqlite3.Connection) -> bool:
    """Whether the aggregate columns exist; older rungs of the migration ladder run plans code first.

    The answer is kept per connection until its schema changes (``PRAGMA schema_version``).
    """
    version = connection.execute("PRAGMA schema_version").fetchone()[0]
    if _READY.get(connection) == version:
        return True
    # The statement form: a table-valued ``pragma_table_info(...)`` traces as ``PRAGMA table_info='...'``,
    # which a lane's write classifier reads as a PRAGMA assignment.
    found = any(row[1] == "sub_open" for row in connection.execute("PRAGMA table_info(plan_nodes)"))
    if found:
        _READY[connection] = version
    return found


def open_siblings_sql(connection: sqlite3.Connection, alias: str = "plan_nodes") -> str:
    """The sibling rows a position names: the plan's open work, through ``idx_plan_nodes_open``.

    Order among siblings is only ever read among open rows (the pane lists the rest by when
    they folded), so a reorder or an insert renumbers these and leaves settled siblings'
    stored keys alone. Below the aggregates schema every unarchived sibling counts.
    """
    if ready(connection):
        return node_states.listed_open_sql(alias)
    return node_states.unarchived_sql(alias)


def apply(connection: sqlite3.Connection, node_id: str, *, at: str) -> None:
    """Re-derive ``node_id`` and push what changed to every ancestor and the plan.

    At each ancestor the direct-child count is adjusted and its own contribution
    recomputed before the delta is tested for zero, since its leafness or container
    status can change with a zero incoming delta. The walk also carries the change
    in each node's fold tone to its parent's ``child_folded_*``, so it stops only
    when the propagated delta and the pending tone change are both zero.
    """
    if not ready(connection):
        return
    chain = [dict(row) for row in connection.execute(_CHAIN_SQL, (node_id,))]
    if not chain:
        return
    chain = node_states.live_chain(chain)
    head = chain[0]
    d_open, d_work, d_folded, child_delta = refresh(head, at)
    listed = int(head["archived_at"] is None)
    # ``top_roots`` counts rows with no parent at all (rebuild, recount); a row stranded
    # under an archived parent is a root for the figures but never for this count.
    roots = listed - head["listed"] if head["parent_id"] is None else 0
    head["listed"] = listed
    if head["own_last_changed"] is None:
        head["own_last_changed"] = head["updated_at"]
    visited = [head]
    reached_top = True
    for parent in chain[1:]:
        if not (d_open or any(d_work) or child_delta or any(d_folded)):
            reached_top = False
            break
        parent["child_in_work"] += child_delta
        child_delta = 0
        for tone, change in zip(TONES, d_folded):
            parent[f"child_folded_{tone}"] += change
        parent["sub_open"] += d_open
        for name, change in zip(WORK_NAMES, d_work):
            parent[f"sub_{name}"] += change
        d_open, d_work, d_folded, _ = refresh(parent, at)
        visited.append(parent)
    if reached_top:
        _push_to_plan(connection, head["plan_id"], d_open, d_work, d_folded, roots)
    else:
        _push_to_plan(connection, head["plan_id"], 0, NO_WORK, NO_FOLDED, roots)
    persist(connection, visited)


def _push_to_plan(
    connection: sqlite3.Connection, plan_id: str, d_open: int, d_work: Work, d_folded: Folded, roots: int,
) -> None:
    """Add the top-level deltas to the plan's counters; no statement when nothing moved."""
    deltas = (d_open, *d_work, roots, *d_folded)
    if not any(deltas):
        return
    assignments = ", ".join(f"{name}={name}+?" for name in _PLAN_COLUMNS)
    connection.execute(f"UPDATE plans SET {assignments} WHERE id=?", (*deltas, plan_id))


#: What ``plans.settled_events`` counts: an event that moved a node to one of these.
SETTLED_EVENT_STATES = ("accepted", "accepted_with_followup", "failed")


def settled_events(connection: sqlite3.Connection, plan: Mapping) -> int:
    """How many settling events a plan has had: the counter, or a count before the counter exists."""
    if ready(connection):
        return int(plan["settled_events"])
    marks = ",".join("?" for _ in SETTLED_EVENT_STATES)
    return connection.execute(
        "SELECT COUNT(*) FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id "
        f"WHERE n.plan_id=? AND e.new_state IN ({marks})", (plan["id"], *SETTLED_EVENT_STATES),
    ).fetchone()[0]


def note_event(connection: sqlite3.Connection, node_id: str, new_state: str | None = None) -> None:
    """Set a node's last-changed time to its latest event, and count a settling one.

    ``service._event`` calls this for every event it writes, the only runtime writer.
    """
    if not ready(connection):
        return
    if new_state in SETTLED_EVENT_STATES:
        writer_context.execute(
            connection,
            "UPDATE plans SET settled_events=settled_events+1 "
            "WHERE id=(SELECT plan_id FROM plan_nodes WHERE id=?)",
            (node_id,),
        )
    writer_context.execute(
        connection, "UPDATE plan_nodes SET own_last_changed="
        "(SELECT MAX(created_at) FROM plan_node_events WHERE node_id=?) WHERE id=?",
        (node_id, node_id),
    )
