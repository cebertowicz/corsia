"""The one loader for a plan read that costs open work, never settled history (#731 S2-1).

``load`` returns the rows a reader of one plan can act on, in canonical plan order
with their ancestor paths: every open row (``idx_plan_nodes_open``), the settled
leaves of a recent window when the reader lists them, and the ancestors those rows
climb through. It never reads a settled row except through those two indexes and the
ancestor walk, so its cost is the open work, the window and the plan's depth.

What the missing settled children would have said comes from the stored aggregates
(``plans.aggregates``), carried on every row: ``child_in_work`` for who is a parent,
``folded`` for who folds, ``sub_done`` / ``sub_failed`` for the leaves beneath. The
``Shape`` derives from them the answers ``plans.node_states`` gives from a full row
list (``parent_ids``, ``leaves``, ``folded_ids``, ``in_play``); the equivalence
property test holds the two equal.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from infra.packed_rows import fetch_all
from plans import errors, node_states

#: Ids per ``IN (...)`` list; SQLite's host-parameter limit binds the chunk, not the plan.
_ID_CHUNK = 400

_TONES = ("done", "failed", "closed")
_UNARCHIVED = node_states.unarchived_sql("plan_nodes")
_OPEN_SQL = f"SELECT * FROM plan_nodes WHERE plan_id=? AND {node_states.listed_open_sql('plan_nodes')}"
#: Settled rows changed since a cutoff, through ``idx_plan_nodes_recent``: the candidates
#: for what a reader lists as recent work (``load`` keeps the ones that are leaves).
_WINDOW_SQL = (
    f"SELECT * FROM plan_nodes WHERE plan_id=? AND {_UNARCHIVED} AND own_last_changed>=?"
    " AND folded=1 AND sent_back=0 AND state IN ({settled})"
)
#: The newest folded rows directly beneath one parent, through ``idx_plan_nodes_folded``.
_FOLDED_UNDER_SQL = (
    "SELECT * FROM plan_nodes WHERE plan_id=? AND parent_id{parent} AND {unarchived} AND folded=1"
    " AND sent_back=0{since} ORDER BY folded_at DESC, id LIMIT ?"
)
_SETTLED = tuple(sorted(node_states._TERMINAL_STATES))
_ROWS_BY_ID_SQL = f"SELECT * FROM plan_nodes WHERE {_UNARCHIVED} AND id IN ({{marks}})"


@dataclass(frozen=True)
class FoldWindow:
    """The folded rows a pane lists beside the open work: per level, the newest ``cap`` that folded after ``since``.

    ``since`` None (a plan never viewed) lists the newest ``cap`` whatever their age.
    """

    since: str | None
    cap: int = 10


@dataclass(frozen=True)
class Shape:
    """The plan structure a full row list would give, derived from stored aggregates."""

    #: Rows neither retired nor under a retired ancestor (``node_states.work_ids``).
    work: frozenset[str]
    #: Rows in the work with a child in the work (``node_states.parent_ids``).
    parents: frozenset[str]
    #: Rows heading a branch with nothing open left (``node_states.folded_ids``).
    folded: frozenset[str]
    #: Rows with open work in or at them (stored ``folded = 0``).
    open: frozenset[str]
    #: Per row, the settled leaves beneath it in the work: ``(done, failed)``.
    settled_leaves: Mapping[str, tuple[int, int]] = field(default_factory=dict)

    def leaf_ids(self, ids: Iterable[str]) -> list[str]:
        """The given ids that are leaves: in the work with no child in the work."""
        return [node_id for node_id in ids if node_id in self.work and node_id not in self.parents]


@dataclass(frozen=True)
class OpenTree:
    """One plan's open rows (and window rows) in plan order, with the plan's top level."""

    plan: sqlite3.Row
    #: ``(row, ancestor path)`` in canonical plan order, as ``node_order.ordered_nodes_with_paths``.
    listed: tuple[tuple[sqlite3.Row, tuple[str, ...]], ...]
    shape: Shape

    @property
    def rows(self) -> list[sqlite3.Row]:
        return [row for row, _path in self.listed]

    def open_roots(self) -> list[sqlite3.Row]:
        """Open rows that head the plan's tree: no parent among the listed rows."""
        ids = {str(row["id"]) for row in self.rows}
        return [
            row for row in self.rows
            if str(row["id"]) in self.shape.open
            and (row["parent_id"] is None or str(row["parent_id"]) not in ids)
        ]


def leaves(tree: OpenTree) -> list[sqlite3.Row]:
    """The listed rows that are leaves, in plan order (``node_states.leaves``)."""
    leaf = set(tree.shape.leaf_ids(str(row["id"]) for row in tree.rows))
    return [row for row in tree.rows if str(row["id"]) in leaf]


def _root_of(node_id: str, rows: Mapping[str, sqlite3.Row]) -> str:
    seen = {node_id}
    while rows[node_id]["parent_id"] is not None and str(rows[node_id]["parent_id"]) in rows:
        node_id = str(rows[node_id]["parent_id"])
        if node_id in seen:
            break
        seen.add(node_id)
    return node_id


def in_play(tree: OpenTree) -> list[sqlite3.Row]:
    """The listed leaves whose root branch has not folded (``node_states.in_play``)."""
    rows = {str(row["id"]): row for row in tree.rows}
    return [
        leaf for leaf in leaves(tree)
        if _root_of(str(leaf["id"]), rows) not in tree.shape.folded
    ]


def settled_leaves_in_play(tree: OpenTree) -> tuple[int, int]:
    """``(done, failed)`` leaves that no listed row stands for, under roots still in play.

    A listed row in the work holds in ``sub_done`` / ``sub_failed`` every settled leaf
    beneath it; the listed children hold their own share in ``prop_*``, so what is
    left is what the folded, unlisted children passed up.
    """
    rows = {str(row["id"]): row for row in tree.rows}
    listed: defaultdict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows.values():
        parent = row["parent_id"]
        if parent is not None and str(parent) in rows:
            listed[str(parent)][0] += row["prop_done"]
            listed[str(parent)][1] += row["prop_failed"]
    done = failed = 0
    for node_id, row in rows.items():
        if node_id not in tree.shape.work or _root_of(node_id, rows) in tree.shape.folded:
            continue
        held_done, held_failed = listed[node_id]
        done += row["sub_done"] - held_done
        failed += row["sub_failed"] - held_failed
    return done, failed


class UnarchivedIds:
    """Which ids are unarchived nodes of one plan, asked one id at a time.

    Stands for the plan's whole id set where a reader only asks about a few (a
    dependency's target), so the question costs a lookup, not the plan.
    """

    def __init__(self, connection, plan_id: str, listed: Iterable[str] = ()) -> None:
        self._connection, self._plan_id = connection, plan_id
        self._known = set(listed)

    def __contains__(self, node_id: object) -> bool:
        if node_id in self._known:
            return True
        return node_id is not None and self._connection.execute(
            f"SELECT 1 FROM plan_nodes WHERE id=? AND plan_id=? AND {_UNARCHIVED}",
            (node_id, self._plan_id),
        ).fetchone() is not None


class PlanTitles(Mapping):
    """Titles of a plan's unarchived nodes: the listed rows', and any other asked by id.

    Stands for the plan's whole title map where a reader names a few nodes (the target of
    a dependency), so the question costs a lookup, not the plan.
    """

    def __init__(self, connection, plan_id: str, listed: Iterable[sqlite3.Row] = ()) -> None:
        self._connection, self._plan_id = connection, plan_id
        self._known = {str(row["id"]): row["title"] for row in listed}

    def __getitem__(self, node_id: str) -> str:
        if node_id not in self._known:
            row = self._connection.execute(
                f"SELECT title FROM plan_nodes WHERE id=? AND plan_id=? AND {_UNARCHIVED}",
                (node_id, self._plan_id),
            ).fetchone()
            if row is None:
                raise KeyError(node_id)
            self._known[node_id] = row["title"]
        return self._known[node_id]

    def __iter__(self):
        return iter(self._known)

    def __len__(self) -> int:
        return len(self._known)


class PlansTitles(Mapping):
    """The titles of several plans' nodes: each plan's own ``PlanTitles``, asked in turn."""

    def __init__(self, connection, trees: Iterable["OpenTree"]) -> None:
        self._by_plan = [PlanTitles(connection, str(tree.plan["id"]), tree.rows) for tree in trees]

    def __getitem__(self, node_id: str) -> str:
        for titles in self._by_plan:
            try:
                return titles[node_id]
            except KeyError:
                continue
        raise KeyError(node_id)

    def __iter__(self):
        return (node_id for titles in self._by_plan for node_id in titles)

    def __len__(self) -> int:
        return sum(len(titles) for titles in self._by_plan)


def _chunks(ids: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(ids), _ID_CHUNK):
        yield ids[start:start + _ID_CHUNK]


def _fetch_missing(connection, found: dict[str, sqlite3.Row], parents: Iterable) -> None:
    """Load unarchived ancestor rows until every row in ``found`` has its parent or none exists.

    An archived or absent parent ends a chain: the child is then a root, as
    ``plans.aggregates_rebuild`` and the recount treat it.
    """
    wanted = {str(parent) for parent in parents if parent is not None and str(parent) not in found}
    while wanted:
        fetched: list[sqlite3.Row] = []
        for chunk in _chunks(sorted(wanted)):
            marks = ",".join("?" for _ in chunk)
            fetched.extend(fetch_all(connection, _ROWS_BY_ID_SQL.format(marks=marks), chunk))
        wanted = set()
        for row in fetched:
            found[str(row["id"])] = row
            parent = row["parent_id"]
            if parent is not None and str(parent) not in found:
                wanted.add(str(parent))


def _under_sent_back_round(row: sqlite3.Row, found: Mapping[str, sqlite3.Row]) -> bool:
    """Whether a row sits under a sent-back round, which a rollup never lists (``rollup._settlements``)."""
    cursor = row["parent_id"]
    seen: set[str] = set()
    while cursor is not None and str(cursor) in found and str(cursor) not in seen:
        seen.add(str(cursor))
        if found[str(cursor)]["sent_back"]:
            return True
        cursor = found[str(cursor)]["parent_id"]
    return False


def _order(rows: Mapping[str, sqlite3.Row]) -> list[tuple[sqlite3.Row, tuple[str, ...]]]:
    """Depth-first plan order by ``(order_key, id)`` among siblings, stranded rows last.

    The order ``node_order.ordered_nodes_with_paths`` gives a full plan, restricted
    to these rows: a row whose parent is not listed is unreachable from a root and
    follows the tree, flat, with no path.
    """
    children: dict[str | None, list[str]] = defaultdict(list)
    for node_id, row in rows.items():
        parent = row["parent_id"]
        children[None if parent is None else str(parent)].append(node_id)
    for siblings in children.values():
        siblings.sort(key=lambda node_id: (rows[node_id]["order_key"], node_id))
    ordered: list[tuple[sqlite3.Row, tuple[str, ...]]] = []
    reached: set[str] = set()
    stack: list[tuple[str, tuple[str, ...]]] = [(node_id, ()) for node_id in reversed(children[None])]
    while stack:
        node_id, path = stack.pop()
        if node_id in reached:
            continue
        reached.add(node_id)
        ordered.append((rows[node_id], path))
        stack.extend((child, (*path, node_id)) for child in reversed(children.get(node_id, [])))
    stranded = sorted(
        (node_id for node_id in rows if node_id not in reached),
        key=lambda node_id: (rows[node_id]["order_key"], node_id),
    )
    return ordered + [(rows[node_id], ()) for node_id in stranded]


def _shape(rows: Mapping[str, sqlite3.Row]) -> Shape:
    work = node_states.work_ids(rows.values())
    parents = {node_id for node_id in work if rows[node_id]["child_in_work"] > 0}
    return Shape(
        work=frozenset(work),
        parents=frozenset(parents),
        folded=frozenset(node_id for node_id, row in rows.items() if row["folded"]),
        open=frozenset(node_id for node_id, row in rows.items() if not row["folded"]),
        settled_leaves={
            node_id: (row["sub_done"], row["sub_failed"]) for node_id, row in rows.items()
        },
    )


def _fold_window_rows(
    connection, plan, found: Mapping[str, sqlite3.Row], window: FoldWindow,
) -> list[sqlite3.Row]:
    """The window's folded rows beneath the plan's top level and each open row, newest first per level.

    One index range per level that has folded children (their stored counters say so), each
    stopping at ``cap``, so the cost follows the open levels and not the folded history.
    """
    levels: list[tuple[str | None, bool]] = [(None, any(plan[f"top_folded_{tone}"] for tone in _TONES))]
    levels += [
        (node_id, any(row[f"child_folded_{tone}"] for tone in _TONES))
        for node_id, row in found.items() if not row["folded"]
    ]
    since = " AND folded_at>?" if window.since is not None else ""
    rows: list[sqlite3.Row] = []
    for parent, has_folded in levels:
        if not has_folded:
            continue
        sql = _FOLDED_UNDER_SQL.format(
            parent=" IS NULL" if parent is None else "=?", unarchived=_UNARCHIVED, since=since,
        )
        params = (plan["id"], *(() if parent is None else (parent,)), *((window.since,) if since else ()), window.cap)
        rows.extend(fetch_all(connection, sql, params))
    return rows


def load(
    connection, plan_id: str, *, recent_since: str | None = None, include: Iterable[str] = (),
    fold_window: FoldWindow | None = None,
) -> OpenTree:
    """Load one plan's open rows, the settled leaves changed since ``recent_since``, and their ancestors.

    ``include`` adds the named rows of the plan, settled or not, with their ancestors: a
    read of one node lists it whatever its state. ``fold_window`` lists, beside the open
    rows, the folded rows a pane shows (``FoldWindow``).
    """
    plan = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise errors.UnknownPlan(f"unknown plan: {plan_id}")
    found: dict[str, sqlite3.Row] = {
        str(row["id"]): row for row in fetch_all(connection, _OPEN_SQL, (plan_id,))
    }
    _fetch_missing(connection, found, [row["parent_id"] for row in list(found.values())])
    wanted = [str(node_id) for node_id in include if str(node_id) not in found]
    for chunk in _chunks(wanted):
        marks = ",".join("?" for _ in chunk)
        for row in fetch_all(
            connection, _ROWS_BY_ID_SQL.format(marks=marks) + " AND plan_id=?", (*chunk, plan_id),
        ):
            found[str(row["id"])] = row
    _fetch_missing(connection, found, [row["parent_id"] for row in list(found.values())])
    if fold_window is not None:
        for row in _fold_window_rows(connection, plan, found, fold_window):
            found.setdefault(str(row["id"]), row)
    if recent_since is not None:
        window_sql = _WINDOW_SQL.format(settled=",".join(f"'{state}'" for state in _SETTLED))
        candidates = [
            row for row in fetch_all(connection, window_sql, (plan_id, recent_since))
            if str(row["id"]) not in found
        ]
        known = dict(found)
        known.update((str(row["id"]), row) for row in candidates)
        _fetch_missing(connection, known, [row["parent_id"] for row in candidates])
        work = node_states.work_ids(known.values())
        for row in candidates:
            node_id = str(row["id"])
            # Listed as a leaf: not a parent in the work (``status._facts``), and in no sent-back round.
            if (node_id in work and row["child_in_work"] > 0) or _under_sent_back_round(row, known):
                continue
            found[str(row["id"])] = row
            cursor = row["parent_id"]
            while cursor is not None and str(cursor) in known and str(cursor) not in found:
                found[str(cursor)] = known[str(cursor)]
                cursor = known[str(cursor)]["parent_id"]
    return OpenTree(plan=plan, listed=tuple(_order(found)), shape=_shape(found))


def load_node(connection, plan_id: str, node_id: str) -> OpenTree:
    """One row of a plan and its ancestors, nothing else: a read of a single node.

    The row is listed whatever its state. No open work, window or sibling is loaded,
    so the cost is the row and the plan's depth; the node's own aggregates stand for
    the rows beneath it, as in ``load``. Raises ``errors.UnknownPlan`` for an unknown
    plan and ``KeyError`` when the node is not an unarchived row of it.
    """
    plan = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise errors.UnknownPlan(f"unknown plan: {plan_id}")
    row = connection.execute(
        _ROWS_BY_ID_SQL.format(marks="?") + " AND plan_id=?", (node_id, plan_id),
    ).fetchone()
    if row is None:
        raise KeyError(node_id)
    found: dict[str, sqlite3.Row] = {str(row["id"]): row}
    _fetch_missing(connection, found, [row["parent_id"]])
    return OpenTree(plan=plan, listed=tuple(_order(found)), shape=_shape(found))
