"""Recount for the plan-aggregates design: what today's code shows, by recount.

The independent check the stored aggregates must equal: the 103 -> 104 migration
asserts it before it commits, and the tests use it as their oracle
(``tests/plan_aggregates_oracle.py`` re-exports it). Given one plan's rows (and the ids of nodes
that carry a ``review_rejected`` event), ``recount`` returns for every node and
for the plan's top level the figures the design stores as aggregates:

``open``      nodes that are not folded, as ``node_states.folded`` /
              ``folded_ids`` decide (all-descendants rule);
``done``      accepted leaves in the work, ``operator_node_state`` bucket "done";
``failed``    failed leaves in the work, bucket "failed";
``closed``    closed nodes, excluding sent-back rounds (``rollup``'s
              ``rejected_round_sql``);
``in_work``   nodes in the work (``node_states.work_ids``).

It calls the existing functions for every classification: ``work_ids``,
``leaves``, ``parent_ids``, ``folded``, ``is_container``, ``folded_ids``,
``retired`` and ``leaf_bucket_from_facts``. Only the subtree sums are loops
written here, because no existing function sums over a subtree.

Rule choices (each one a place where the design text left a rule open; every
one is also reported to the design owner, none is resolved silently):

R1. *Archived rows.* They are dropped before anything is computed, exactly as
    ``tree_rows.many`` / ``service._plan_tree_rows`` load only unarchived rows.
    So an archived node contributes nothing and is not a child of anything.
R2. *``open`` is the node's own fold answer summed over the subtree.* A node's
    ``own_open`` is ``not node_states.folded(...)`` (its row, with ``is_container``
    taking ``parent_ids`` membership). ``sub_open == 0`` then makes the all-
    descendants rule: ``folded(N) == own_folded(N) and sub_open(N) == 0``, which
    ``recount`` asserts equals ``N in folded_ids(rows)`` for every node.
R3. *``open`` ignores retirement.* It sums the whole subtree, including below a
    closed node, because ``folded_subtrees`` does.
R4. *Work counts cross a retired node by its own contribution only.* A closed
    node counts 1 closed and 0 in_work for its parent; nothing beneath it counts
    (``work_ids`` puts the whole subtree out of the work). This is the design's
    "closed counts are cut at a retired ancestor", and it is a choice because
    today's pane still lists closed rows under a closed phase.
R5. *A closed node counts as closed for its ancestors when its parent is in the
    work* (or it is a root). A closed node under a closed node counts nowhere.
R6. *Sent-back.* ``sent_back_ids = {id : review_rejected event and state is
    closed}``, the ``rejected_round_sql`` predicate. A sent-back node is never
    counted as closed. Its descendants are already cut by R4.
R7. *``done`` / ``failed`` are leaves in the work only* (``node_states.leaves``),
    bucketed by ``leaf_bucket_from_facts``; a settled parent with a child in
    work is no leaf and counts in neither, and a settled leaf under a retired
    ancestor counts nowhere. Whether a root branch is folded does not matter
    here: ``progress_done`` below applies the in-play filter separately.
R8. *What is compared for a node.* ``fold`` figures (``open``) are defined for
    every node. Work figures of a node are defined as what that node *passes
    to its parent*: its full subtree sum when it is in the work, only its own
    closed count when it is retired, and nothing is defined for a node under a
    retired ancestor (nothing is ever read from there).
R10. *Fold-row counts* (r4): the folded, unarchived, non-sent-back direct
    children of a node, by the tone each shows (``foldLabel``: ``tone_key``
    complete / failed / anything else is "closed"). The tone is the server's
    own: a folded *container* is read through ``branch_display`` over its work
    leaves' ``leaf_bucket_from_facts`` distribution (a settled leaf is neither
    working nor needs-user, so both flags are False); every other folded row
    shows ``display_from_facts`` of its own state, so a folded accepted phase is
    "complete" whatever its children are. Levels the pane never lists (a
    sent-back round and anything beneath it) are not compared.
R9. *Top level.* Roots are nodes with no parent in the loaded rows. Top-level
    work figures are the sum of what the roots pass up; ``open`` is the sum
    over all roots. ``progress_done`` is ``len`` of accepted leaves among
    ``node_states.in_play`` (the leaves whose root branch has not folded), the
    figure ``projections._plan_state`` shows as ``done``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from plans import aggregates, node_states
from plans.operator_display_state import display_from_facts
from plans.operator_node_state import DISTRIBUTION_KEYS, leaf_bucket_from_facts
from plans.operator_nodes import branch_display


@dataclass(frozen=True)
class Counts:
    """The five recounted figures."""

    open: int = 0
    done: int = 0
    failed: int = 0
    closed: int = 0
    in_work: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(
            self.open + other.open, self.done + other.done, self.failed + other.failed,
            self.closed + other.closed, self.in_work + other.in_work,
        )


@dataclass(frozen=True)
class NodeFigures:
    """What the oracle says about one node."""

    own_folded: bool
    folded: bool
    #: Whether ``node_states.work_ids`` puts the node in the work.
    in_work: bool
    child_in_work: int
    sub_open: int
    #: ``int(not own_folded) + sub_open``: the fold figure the node passes up (R3).
    open_total: int
    #: Folded direct children by tone (R10): (done, failed, closed).
    child_folded: tuple[int, int, int]
    #: Whether the pane lists this node's level: not a sent-back round or beneath one.
    visible: bool
    #: What the node passes to its parent (R8); ``None`` under a retired ancestor.
    passed: Counts | None
    #: The tone a folded, non-sent-back row shows (R10), else ``None``: the stored ``fold_bucket``.
    fold_tone: str | None = None


@dataclass(frozen=True)
class Recount:
    nodes: dict[str, NodeFigures]
    top: Counts
    top_folded: tuple[int, int, int]
    progress_done: int


class _Tree:
    """The rows of one plan, read the way the pane and the recount read them."""

    def __init__(self, rows: Iterable[Mapping], review_rejected_ids: Iterable[str]) -> None:
        live = [dict(row) for row in rows if row.get("archived_at") is None]  # R1
        self.live = live
        self.by_id = {str(row["id"]): row for row in live}
        self.children: dict[str, list[str]] = {node_id: [] for node_id in self.by_id}
        for row in live:
            if node_states.has_live_parent(row, self.by_id):
                self.children[str(row["parent_id"])].append(str(row["id"]))
        self.work = node_states.work_ids(live)
        self.parents = node_states.parent_ids(live)
        self.leaf_ids = {str(row["id"]) for row in node_states.leaves(live)}
        self.folded_set = node_states.folded_ids(live)
        rejected = {str(node_id) for node_id in review_rejected_ids}
        self.sent_back = {
            node_id for node_id in rejected & self.by_id.keys() if self.by_id[node_id]["state"] == "closed"
        }  # R6

    def own_folded(self, node_id: str) -> bool:
        row = self.by_id[node_id]
        return node_states.folded(
            row["state"], row.get("archived_at"),
            container=node_states.is_container(row, node_id in self.parents),
        )

    def descendants(self, node_id: str) -> list[str]:
        found: list[str] = []
        stack = list(self.children[node_id])
        while stack:
            current = stack.pop()
            found.append(current)
            stack.extend(self.children[current])
        return found

    def parent_in_work(self, node_id: str) -> bool:
        parent = self.by_id[node_id]["parent_id"]
        return parent is None or str(parent) not in self.by_id or str(parent) in self.work

    def is_root(self, node_id: str) -> bool:
        return not node_states.has_live_parent(self.by_id[node_id], self.by_id)

    def own_work(self, node_id: str) -> Counts:
        """The node's own work figures, as a contribution to a parent in the work."""
        row = self.by_id[node_id]
        done = failed = 0
        if node_id in self.leaf_ids:  # leaves() already implies in the work (R7)
            bucket = leaf_bucket_from_facts(str(row["state"]), "ready", display_label="ready")
            done = int(bucket == "done")
            failed = int(bucket == "failed")
        closed = int(
            row["state"] == node_states.CLOSED and node_id not in self.sent_back
            and self.parent_in_work(node_id)
        )
        return Counts(0, done, failed, closed, int(node_id in self.work))

    def passed(self, node_id: str) -> Counts | None:
        if node_id in self.work:
            total = self.own_work(node_id)
            for child in self.descendants(node_id):
                # A descendant counts when everything strictly above it, up to
                # this node, is in the work: its parent is in the work (R4, R5).
                if self.parent_in_work(child) and (
                    child in self.work or self.by_id[child]["state"] == node_states.CLOSED
                ):
                    total = total + self.own_work(child)
            return total
        if node_states.retired(self.by_id[node_id]) and self.parent_in_work(node_id):
            return self.own_work(node_id)
        return None

    def tone(self, node_id: str) -> str:
        """``foldLabel``'s bucket for one folded row, from the server's display chain."""
        row = self.by_id[node_id]
        if node_states.is_container(row, node_id in self.parents):
            counts = dict.fromkeys(DISTRIBUTION_KEYS, 0)
            for leaf in [node_id, *self.descendants(node_id)]:
                if leaf in self.leaf_ids:
                    bucket = leaf_bucket_from_facts(
                        str(self.by_id[leaf]["state"]), "ready", display_label="ready",
                    )
                    counts[bucket] += 1
            display = branch_display(counts, needs_user=False, working=False)
        else:
            display = display_from_facts(str(row["state"]), "ready")
        return {"complete": "done", "failed": "failed"}.get(display["tone_key"], "closed")

    def level(self, child_ids: Iterable[str]) -> tuple[int, int, int]:
        tones = [self.tone(c) for c in child_ids if c in self.folded_set and c not in self.sent_back]
        return tones.count("done"), tones.count("failed"), tones.count("closed")

    def visible(self, node_id: str) -> bool:
        cursor: str | None = node_id
        while cursor is not None and cursor in self.by_id:
            if cursor in self.sent_back:
                return False
            parent = self.by_id[cursor]["parent_id"]
            cursor = None if parent is None else str(parent)
        return True

    def figures(self, node_id: str) -> NodeFigures:
        sub_open = sum(int(not self.own_folded(child)) for child in self.descendants(node_id))
        folded = self.own_folded(node_id) and sub_open == 0
        assert folded == (node_id in self.folded_set), (node_id, folded, self.folded_set)  # R2
        return NodeFigures(
            own_folded=self.own_folded(node_id),
            folded=folded,
            in_work=node_id in self.work,
            child_in_work=sum(int(child in self.work) for child in self.children[node_id]),
            sub_open=sub_open,
            open_total=int(not self.own_folded(node_id)) + sub_open,
            child_folded=self.level(self.children[node_id]),
            visible=self.visible(node_id),
            passed=self.passed(node_id),
            fold_tone=self.tone(node_id) if folded and node_id not in self.sent_back else None,
        )


def recount(rows: Iterable[Mapping], review_rejected_ids: Iterable[str] = ()) -> Recount:
    tree = _Tree(rows, review_rejected_ids)
    nodes = {node_id: tree.figures(node_id) for node_id in tree.by_id}
    roots = [node_id for node_id in tree.by_id if tree.is_root(node_id)]
    top = Counts()
    for node_id in roots:
        top = top + (nodes[node_id].passed or Counts()) + Counts(open=nodes[node_id].open_total)
    in_play = node_states.in_play(tree.live)
    progress_done = sum(row["state"] in node_states._ACCEPTED_STATES for row in in_play)
    return Recount(nodes=nodes, top=top, top_folded=tree.level(roots), progress_done=progress_done)


def stored_mismatches(connection: sqlite3.Connection, plan_id: str) -> list[str]:
    """Compare one plan's stored aggregates with a recount; one entry per disagreement.

    Each entry is ``field`` or ``field:node_id``. Compared exactly as the design
    reads them: figures a node passes up only where something reads them (nothing
    reads a node under a retired ancestor), and a fold level only where the pane
    lists it.

    Besides the figures it also compares the columns the readers will rely on:
    ``sent_back`` (a closed node with a ``review_rejected`` event), ``listed``,
    ``own_last_changed`` present, ``folded_at`` set exactly for a folded node,
    ``fold_bucket`` (a folded, unarchived, non-sent-back node's tone, else null)
    and the plan's ``top_roots``. A teardown recount of every test that writes
    plan rows runs this, so a writer that skipped ``aggregates.apply`` fails there.
    """
    rows = [dict(row) for row in connection.execute("SELECT * FROM plan_nodes WHERE plan_id=?", (plan_id,))]
    rejected = [
        str(row[0]) for row in connection.execute(
            "SELECT DISTINCT e.node_id FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id "
            "WHERE n.plan_id=? AND e.event='review_rejected' AND n.state='closed'", (plan_id,),
        )
    ]
    expected = recount(rows, rejected)
    found: list[str] = []
    for row in rows:
        node_id = str(row["id"])
        if bool(row["sent_back"]) != (node_id in rejected):
            found.append(f"sent_back:{node_id}")
        figures = expected.nodes.get(node_id)
        if bool(row["listed"]) != (row["archived_at"] is None):
            found.append(f"listed:{node_id}")
        if row["own_last_changed"] is None:
            found.append(f"own_last_changed:{node_id}")
        if (row["folded_at"] is not None) != bool(figures and figures.folded):
            found.append(f"folded_at:{node_id}")
        if row["fold_bucket"] != (figures.fold_tone if figures else None):
            found.append(f"fold_bucket:{node_id}")
        if figures is None:  # archived: contributes nothing
            if any(row[name] for name in ("prop_open", "prop_done", "prop_failed", "prop_closed", "prop_in_work")):
                found.append(f"archived:{node_id}")
            continue
        checks: dict[str, tuple[Any, Any]] = {
            "prop_open": (row["prop_open"], figures.open_total),
            "sub_open": (row["sub_open"], figures.sub_open),
            "folded": (bool(row["folded"]), figures.folded),
        }
        if figures.in_work:
            checks["child_in_work"] = (row["child_in_work"], figures.child_in_work)
        if figures.visible:
            checks["child_folded"] = (
                (row["child_folded_done"], row["child_folded_failed"], row["child_folded_closed"]),
                figures.child_folded,
            )
        if figures.passed is not None:
            checks["work"] = (
                (row["prop_done"], row["prop_failed"], row["prop_closed"], row["prop_in_work"]),
                (figures.passed.done, figures.passed.failed, figures.passed.closed, figures.passed.in_work),
            )
        found.extend(f"{name}:{node_id}" for name, (stored, wanted) in checks.items() if stored != wanted)
    plan = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    top = {
        "top_roots": (plan["top_roots"], sum(r["parent_id"] is None and r["archived_at"] is None for r in rows)),
        "top_open": (plan["top_open"], expected.top.open),
        "top_work": (
            (plan["top_done"], plan["top_failed"], plan["top_closed"], plan["top_in_work"]),
            (expected.top.done, expected.top.failed, expected.top.closed, expected.top.in_work),
        ),
        "top_folded": (
            (plan["top_folded_done"], plan["top_folded_failed"], plan["top_folded_closed"]),
            expected.top_folded,
        ),
        "settled_events": (plan["settled_events"], _settled_events(connection, plan_id)),
    }
    found.extend(name for name, (stored, wanted) in top.items() if stored != wanted)
    return found


def _settled_events(connection: sqlite3.Connection, plan_id: str) -> int:
    """Events that moved a node of the plan to a settled state, counted from the event table."""
    marks = ",".join("?" for _ in aggregates.SETTLED_EVENT_STATES)
    return connection.execute(
        "SELECT COUNT(*) FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id "
        f"WHERE n.plan_id=? AND e.new_state IN ({marks})",
        (plan_id, *aggregates.SETTLED_EVENT_STATES),
    ).fetchone()[0]
