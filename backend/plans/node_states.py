"""The plan node lifecycle: every state, and which state may follow which.

Split from ``plans.service`` so the state machine is one small table a reader
can take in at once, rather than constants buried at the top of the
transaction core. ``service`` re-exports every name here, so each existing
``service._TERMINAL_STATES``-style reader keeps working unchanged. Like
``plans.errors``, this imports nothing from ``plans``, so it can never be part
of an import cycle.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping

_ACCEPTED_STATES = frozenset({"accepted", "accepted_with_followup"})

#: Work that will not be done, as distinct from work that cannot be: `failed`
#: means the objective could not be met and stays on the plan as that record,
#: while `closed` means it is no longer wanted. `plans.closure` owns the write;
#: see `plans.close_next`, whose "close" is the unrelated act of taking an
#: executor off a task.
CLOSED = "closed"

#: States with no legal successor, so no writer may transition out of them.
_TERMINAL_STATES = frozenset({"accepted", "accepted_with_followup", "failed", CLOSED})

_TRANSITIONS = {
    # pending -> review_requested is a delegated step whose worker returned
    # before the parent started it; only the harvest writes it.
    "pending": frozenset({"active", "review_requested", "failed", CLOSED}),
    "active": frozenset({"review_requested", "failed", CLOSED}),
    "review_requested": frozenset({"active", "accepted", "accepted_with_followup", "failed", CLOSED}),
    "accepted": frozenset(),
    "accepted_with_followup": frozenset(),
    "failed": frozenset(),
    CLOSED: frozenset(),
}


def retired(node: Mapping | sqlite3.Row) -> bool:
    """Whether one node's own row ends its part in the work: archived or closed.

    A closed node stays on the plan (#1887) but is out of the work, exactly as
    an archived one always was, and so is everything beneath it: that is
    ``work_ids``, the rule for a whole plan. On its own this answer is complete
    only for a row whose ancestors cannot be retired -- any open node, since
    nothing open may sit under a closed or archived one
    (``service.require_live_node``, ``archival``'s close guard).
    """
    # ``sqlite3.Row`` has ``keys()`` but no ``get``; a row read without the
    # column is one the caller already filtered to unarchived.
    archived = node["archived_at"] if "archived_at" in node.keys() else None
    return archived is not None or node["state"] == CLOSED


def unarchived_sql(alias: str) -> str:
    """The ``plan_nodes`` row ``alias`` still exists: not archived, any state.

    For reads that must disclose closed work, as the compact Activity index
    does. Every other flat read wants ``in_work_sql``, which also drops closed.
    """
    return f"{alias}.archived_at IS NULL"


def in_work_sql(alias: str) -> str:
    """Not ``retired``, as SQL conditions on the ``plan_nodes`` row ``alias``.

    Row-level, like ``retired``: complete for open rows and for a node's
    direct children. A walk over descendants must stop at a row failing it
    rather than filter after descending (``review_verification``).
    """
    return f"{unarchived_sql(alias)} AND {alias}.state <> '{CLOSED}'"


def asks_user(state: str, assignee: str | None, *, has_follow_up: bool) -> bool:
    """Whether a prerequisite stretch is time the user owns.

    This is the Python replay of ``failure_asks_user_sql``'s rule: a
    user-assigned item stays theirs unless it was accepted or closed; a failed
    item stops being theirs only once a follow-up exists.
    """
    return (
        assignee == "user"
        and state not in _ACCEPTED_STATES
        and state != CLOSED
        and (state != "failed" or not has_follow_up)
    )


def failure_asks_user_sql(alias: str) -> str:
    """The failed ``plan_nodes`` row ``alias`` still asks the user, as SQL.

    The one rule for "this failure is the user's": the item is failed right
    now (not retried), assigned to the user, in work and outside a retired
    branch, and has no follow-up. ``asks_user`` replays the same rule over
    state history. The chat list's Failed mark and the update dot and
    standalone failure card all read it; each adds only its own seen-state on
    top.
    """
    return (
        f"{alias}.state='failed' AND {alias}.assignee='user'"
        f" AND {in_work_sql(alias)} AND {outside_retired_branch_sql(alias)}"
        f" AND NOT EXISTS (SELECT 1 FROM plan_nodes follow_up"
        f" WHERE follow_up.follow_up_of={alias}.id)"
    )


def open_user_block_sql(hold: str) -> str:
    """The ``plan_node_holds`` row ``hold`` is an open ``blocked`` hold the user owns, as SQL."""
    return f"{hold}.kind='blocked' AND {hold}.owner_kind='user' AND {hold}.cleared_at IS NULL"


def blocked_on_user_sql(alias: str, placed_hold: str) -> str:
    """The ``plan_nodes`` row ``alias`` is blocked on the user right now, as SQL.

    Focus splits a work block on such a card. Two ways a task is blocked on
    the user, both in work and outside a retired branch: the user's own task
    (``assignee='user'``) with any open ``blocked`` hold, and any task,
    whoever it is assigned to, while the open user-owned ``blocked`` hold named
    by ``placed_hold`` (an SQL expression for one hold id) is open. That second
    hold is an agent's user ask (``plans/user_ask.py``) or a legacy hold with no
    ask artifact. A task an agent holds on itself, a thread, an external party
    or a system cause is not blocked on the user, unless the user also owns
    the task. The ask has no other default-view surface: the Attention card
    that once showed it lost its producer when ``external_reviews`` was
    removed.
    """
    return (
        f"({alias}.assignee='user'"
        f" AND EXISTS (SELECT 1 FROM plan_node_holds hold WHERE hold.node_id={alias}.id"
        f" AND hold.kind='blocked' AND hold.cleared_at IS NULL)"
        f" OR EXISTS (SELECT 1 FROM plan_node_holds hold WHERE hold.node_id={alias}.id"
        f" AND hold.id={placed_hold} AND {open_user_block_sql('hold')}))"
        f" AND {in_work_sql(alias)} AND {outside_retired_branch_sql(alias)}"
    )


def open_sql(alias: str) -> str:
    """The ``plan_nodes`` row ``alias`` is unarchived work still to settle.

    Narrower than ``in_work_sql``: accepted and failed rows are excluded too,
    so a wait that a settled row still holds no longer counts.
    """
    settled = ",".join(f"'{state}'" for state in sorted(_TERMINAL_STATES))
    return f"{unarchived_sql(alias)} AND {alias}.state NOT IN ({settled})"


def listed_open_sql(alias: str) -> str:
    """The ``plan_nodes`` row ``alias`` is a row of the plan's open work, as SQL (``idx_plan_nodes_open``).

    The one meaning of "open" for every read of the open work: the pane lists exactly
    these rows, and the chat list asks whether the plan has any. It is the stored fold
    answer, so a pending container whose children have all settled is not open, and a
    settled row with open work beneath it is.
    """
    return f"{unarchived_sql(alias)} AND {alias}.folded = 0"


def outside_retired_branch_sql(alias: str) -> str:
    """No ancestor of the ``plan_nodes`` row ``alias`` is retired, as SQL.

    For flat reads of settled rows, which ``in_work_sql`` alone would let
    through from under a closed parent. Pair it with ``in_work_sql(alias)``.
    """
    return (
        f"NOT EXISTS (WITH RECURSIVE up(id,parent_id,archived_at,state) AS ("
        f" SELECT a.id,a.parent_id,a.archived_at,a.state FROM plan_nodes a"
        f" WHERE a.id={alias}.parent_id"
        f" UNION ALL SELECT a.id,a.parent_id,a.archived_at,a.state"
        f" FROM plan_nodes a JOIN up ON a.id=up.parent_id)"
        f" SELECT 1 FROM up WHERE NOT ({in_work_sql('up')}))"
    )


def work_ids(rows: Iterable[Mapping | sqlite3.Row]) -> set[str]:
    """The ids of one plan's rows that are in the work.

    A row is in the work when neither it nor any ancestor among ``rows`` is
    ``retired``, so a failed task under a closed phase counts nowhere, as it
    would not had the phase been archived. The one rule every whole-plan reader
    shares, through ``parent_ids`` and ``leaves``. A cyclic chain terminates.
    """
    by_id = {str(row["id"]): row for row in rows}
    answer: dict[str, bool] = {}
    for node_id in by_id:
        chain: list[str] = []
        cursor: str | None = node_id
        result = True
        # Climb until an answer is known, a retired row decides it, or the
        # chain leaves ``rows`` (or loops); everything climbed shares it.
        while cursor in by_id and cursor not in chain:
            if cursor in answer:
                result = answer[cursor]
                break
            chain.append(cursor)
            if retired(by_id[cursor]):
                result = False
                break
            parent = by_id[cursor]["parent_id"]
            cursor = None if parent is None else str(parent)
        for visited in chain:
            answer[visited] = result
    return {node_id for node_id, in_work in answer.items() if in_work}


def parent_ids(rows: Iterable[Mapping | sqlite3.Row]) -> set[str]:
    """The one rule for who is a parent: a node with a child in work.

    Every reader that splits a plan into parents and leaves -- leaves,
    containers, folds, branch counts, next and open tasks, the status read --
    asks this, so a closed child hands its parent back as a leaf everywhere
    at once. ``has_child_in_work_sql`` is the same rule for SQL readers.
    """
    rows = list(rows)
    work = work_ids(rows)
    listed = {
        str(row["parent_id"]) for row in rows
        if row["parent_id"] is not None and str(row["id"]) in work
    }
    # A row of ``plans.open_rollup`` says so itself when its children are not among ``rows``.
    return listed | {
        str(row["id"]) for row in rows
        if str(row["id"]) in work and "has_child_in_work" in row.keys() and row["has_child_in_work"]
    }


def has_live_parent(node: Mapping | sqlite3.Row, live_ids: Iterable[str] | Mapping) -> bool:
    """Whether the node hangs from a live parent; a row under an archived or absent one is a root.

    The one root rule: the incremental aggregate write, the rebuild and the recount all ask it.
    ``live_ids`` holds the ids of the plan's unarchived rows.
    """
    parent = node["parent_id"]
    return parent is not None and str(parent) in live_ids


def live_chain(chain: list[Mapping]) -> list[Mapping]:
    """A node and its ancestors (nearest first), cut where the next ancestor is archived."""
    live = {str(row["id"]) for row in chain if row["archived_at"] is None}
    kept = chain[:1]
    for child, parent in zip(chain, chain[1:]):
        if not has_live_parent(child, live):
            break
        kept.append(parent)
    return kept


def has_child_in_work_sql(alias: str) -> str:
    """``parent_ids`` as a SQL condition on the ``plan_nodes`` row ``alias``.

    Row-level on the child is complete for the open ``alias`` rows its readers
    ask about: an open node has no retired ancestor, so the child's only
    ancestor that could retire it is itself. A child always shares its
    parent's plan (``node_creation._validate_parent``); naming the plan lets
    SQLite use the ``(plan_id, parent_id)`` index instead of scanning every node.
    """
    return (
        f"EXISTS (SELECT 1 FROM plan_nodes work_child"
        f" WHERE work_child.plan_id={alias}.plan_id"
        f" AND work_child.parent_id={alias}.id AND {in_work_sql('work_child')})"
    )


def is_container_sql(alias: str) -> str:
    """``is_container`` as a SQL condition on the ``plan_nodes`` row ``alias``."""
    return (
        f"({has_child_in_work_sql(alias)} AND {alias}.state='pending'"
        f" AND {alias}.spawned_thread_id IS NULL)"
    )


def is_container(node: Mapping, has_children: bool) -> bool:
    """The one rule: a never-started (no transition returns to ``pending``),
    workerless parent. It has no obligation of its own and reads as its leaves."""
    return has_children and node["state"] == "pending" and node.get("spawned_thread_id") is None


def folded(state: str, archived_at: str | None, *, container: bool) -> bool:
    """Return whether one node, on its own, has nothing left to do.

    Settled work (done, failed or closed) has nothing left, and neither does a
    ``container`` (``is_container``). Folding writes nothing: a folded node
    stays on the plan, one expansion away, and only an explicit archive takes
    it off.
    """
    return archived_at is None and (state in _TERMINAL_STATES or container)


def folded_subtrees(
    parents: Mapping[str, str | None], own: Mapping[str, bool],
) -> set[str]:
    """Return the ids that fold in a tree, given each node's own ``folded`` answer.

    A node folds only when it and every descendant fold, so finished parents
    never hide open work beneath them. A cyclic parent chain terminates.
    """
    unfoldable: set[str] = set()
    for node_id, is_folded in own.items():
        if is_folded:
            continue
        parent = parents.get(node_id)
        while parent is not None and parent not in unfoldable:
            unfoldable.add(parent)
            parent = parents.get(parent)
    return {
        node_id for node_id, is_folded in own.items()
        if is_folded and node_id not in unfoldable
    }


def folded_ids(nodes: Iterable[Mapping]) -> set[str]:
    """Fold one plan's nodes: the single rule the pane, the reads and progress share.

    A folded node heads a branch with no open work left in it, and it folds
    the moment that becomes true; the pane's fold wave is where the user sees
    it go. Each node carries ``id``, ``parent_id``, ``state``,
    ``spawned_thread_id`` and, when it can be archived, ``archived_at``.
    """
    rows = list(nodes)
    parents: dict[str, str | None] = {str(node["id"]): node["parent_id"] for node in rows}
    with_children = parent_ids(rows)
    own = {
        str(node["id"]): folded(
            node["state"], node.get("archived_at"),
            container=is_container(node, str(node["id"]) in with_children),
        )
        for node in rows
    }
    return folded_subtrees(parents, own)


def leaves(rows: list) -> list:
    """Return the leaves -- work with no child in work -- of one plan's rows.

    The one definition of "the leaves of a plan": progress, liveness, the
    return wake and the harvest all read it, in the rows' plan order. Nothing
    out of the work is a leaf (``work_ids``): not a closed node, and not what
    sits beneath one.
    """
    work = work_ids(rows)
    parents = parent_ids(rows)
    return [row for row in rows if str(row["id"]) in work and str(row["id"]) not in parents]


def in_play(rows: list) -> list:
    """Return the leaves whose root branch has not folded.

    A root branch leaves the progress count the moment it folds, so the ratio
    is about work still in play: a fully finished plan reads 0/0 while its
    folded nodes stay on the plan, one expansion away.
    """
    fold = folded_ids(dict(row) for row in rows)
    parents = {row["id"]: row["parent_id"] for row in rows}

    def root(node_id: str) -> str:
        seen = {node_id}
        while parents.get(node_id) in parents and parents[node_id] not in seen:
            node_id = parents[node_id]
            seen.add(node_id)
        return node_id

    return [leaf for leaf in leaves(rows) if root(leaf["id"]) not in fold]
