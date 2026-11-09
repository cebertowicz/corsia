"""The title maps a plan read hands its readers: one plan's, and several plans' together.

Lives beside ``open_tree`` rather than inside it because a reader that names a few nodes (the
target of a dependency, the holder of an ask) should pay for those nodes only, and the
question "is this id one of these plans' live nodes?" has to be answered once for the whole
read, not once per plan. ``plans.status_facts`` and ``plans.operator_projection`` build these
and pass them as the read's title map.

``PlansTitles.__contains__`` is the reason this read is cheap. A chat list of 80 threads
asked that question through ``Mapping``'s default membership, which calls ``__getitem__``,
which asks every plan of the read in turn: 912 statements for a question with one answer,
and most of them returned nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from plans import node_states

if TYPE_CHECKING:
    from plans.open_tree import OpenTree

_UNARCHIVED = node_states.unarchived_sql("plan_nodes")


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

    def holds(self, node_id: object) -> bool:
        """Whether this plan already has the node's title, asking nothing of the database."""
        return node_id in self._known

    def remember(self, node_id: str, title: str) -> None:
        """Keep a title read for the same plan elsewhere, so asking for it costs nothing."""
        self._known[node_id] = title

    def __iter__(self):
        return iter(self._known)

    def __len__(self) -> int:
        return len(self._known)


class PlansTitles(Mapping):
    """The titles of several plans' nodes: each plan's own ``PlanTitles``, asked in turn."""

    def __init__(self, connection, trees: Iterable["OpenTree"]) -> None:
        self._connection = connection
        self._plans: list[tuple[str, PlanTitles]] = []
        for tree in trees:
            plan_id = str(tree.plan["id"])
            self._plans.append((plan_id, PlanTitles(connection, plan_id, tree.rows)))
        self._absent: set[str] = set()  # ids asked and found under none of these plans

    def __contains__(self, node_id: object) -> bool:
        """Answer membership in one statement, which ``Mapping`` would answer in one per plan.

        This asks the same question ``__getitem__`` asks -- an unarchived node of any of
        these plans -- across all of them at once, and remembers an id it did not find, so a
        reader that probes the same id twice pays once. A node it does find has its title
        kept where ``__getitem__`` looks first, so asking for it afterwards costs nothing.
        """
        if any(titles.holds(node_id) for _plan_id, titles in self._plans):
            return True
        if not isinstance(node_id, str) or node_id in self._absent or not self._plans:
            return False
        marks = ",".join("?" for _ in self._plans)
        row = self._connection.execute(
            f"SELECT id,plan_id,title FROM plan_nodes WHERE id=? AND plan_id IN ({marks})"
            f" AND {_UNARCHIVED}",
            (node_id, *(plan_id for plan_id, _titles in self._plans)),
        ).fetchone()
        if row is None:
            self._absent.add(node_id)
            return False
        for plan_id, titles in self._plans:
            if plan_id == str(row["plan_id"]):
                titles.remember(str(row["id"]), row["title"])
        return True

    def __getitem__(self, node_id: str) -> str:
        for _plan_id, titles in self._plans:
            try:
                return titles[node_id]
            except KeyError:
                continue
        raise KeyError(node_id)

    def __iter__(self):
        return (node_id for _plan_id, titles in self._plans for node_id in titles)

    def __len__(self) -> int:
        return sum(len(titles) for _plan_id, titles in self._plans)
