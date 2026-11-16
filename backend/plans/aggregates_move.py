"""Move one node's stored figures along the branch it left and the one it joined.

``plans.aggregates.apply`` re-derives what a *row* change moves: it measures the
node's own figures against what it stored, and pushes the difference up. A
reparent changes nothing of the moved node's own -- its subtree comes with it --
so ``apply`` measures zero and pushes nothing, while the branch it left goes on
counting a child it no longer has and the branch it joined never learns of the
new one. That is why a move is the one aggregate change the row hook cannot do
on its own, and why ``plans.regroup`` asks for the two adjustments here.

Each level gets the step ``apply`` takes at each ancestor: absorb the incoming
change, then ``aggregates.refresh`` recomputes that level and reports its own
change to carry on. The walk stops where ``apply`` stops, for the reason
``apply`` gives: a retired ancestor absorbs its children's figures and passes
only its own, so a zero delta there changes nothing above it.

``plans.aggregates_recount.stored_mismatches`` is the check on the result, and
``conftest`` runs it after every test that writes plan rows.
"""

from __future__ import annotations

import sqlite3

from plans import aggregates


def _figures(row, sign: int) -> tuple[int, aggregates.Work, aggregates.Folded, int, int]:
    """What one node passes up, signed: its open count, work, fold tone, own work, root flag.

    The stored ``prop_*`` vector *is* what a node passes to its parent (the
    recount's rule R8), so the move needs no recomputation: the node arrives
    carrying exactly what it left with. ``top_roots`` counts only a literally
    parentless row, as ``apply`` and the rebuild both do, which is a different
    question from which level the figures land on.
    """
    return (
        sign * int(row["prop_open"]),
        tuple(sign * int(row[f"prop_{name}"]) for name in aggregates.WORK_NAMES),
        tuple(sign * value for value in aggregates.weight(row["fold_bucket"])),
        sign * int(row["own_in_work"]),
        sign * int(row["parent_id"] is None and bool(row["listed"])),
    )


def shift(
    connection: sqlite3.Connection, plan_id: str, row, *, parent_id: str | None,
    sign: int, at: str,
) -> None:
    """Add (``sign`` 1) or remove (``sign`` -1) one node's figures below ``parent_id``.

    ``parent_id`` is the branch on that side of the move, or None when the level
    above the node is the plan itself -- which is also the rule for a row stranded
    under an archived parent (``node_states.has_live_parent``, the one root rule
    every reader uses).
    """
    d_open, d_work, d_folded, child_delta, roots = _figures(row, sign)
    if parent_id is None:
        aggregates.push_to_plan(connection, plan_id, d_open, d_work, d_folded, roots)
        return
    visited: list[dict] = []
    for ancestor in aggregates.ancestors(connection, parent_id):
        if not (d_open or any(d_work) or child_delta or any(d_folded)):
            break
        ancestor["child_in_work"] += child_delta
        # Only a direct parent counts children; above it the change travels as
        # open and work figures, exactly as it does in `apply`.
        child_delta = 0
        for tone, change in zip(aggregates.TONES, d_folded):
            ancestor[f"child_folded_{tone}"] += change
        ancestor["sub_open"] += d_open
        for name, change in zip(aggregates.WORK_NAMES, d_work):
            ancestor[f"sub_{name}"] += change
        d_open, d_work, d_folded, _ = aggregates.refresh(ancestor, at)
        visited.append(ancestor)
    aggregates.push_to_plan(connection, plan_id, d_open, d_work, d_folded, 0)
    aggregates.persist(connection, visited)


def move(
    connection: sqlite3.Connection, before, after, *, from_parent: str | None,
    to_parent: str | None, at: str,
) -> None:
    """Re-derive both sides of one reparent, in the one write that made it.

    ``before`` is the node's row as it hung from the branch it left and
    ``after`` the same node as it hangs from the branch it joined; only the
    literal ``parent_id`` differs, and each side needs its own for the root
    figure. ``from_parent`` and ``to_parent`` name the levels that hold the
    figures, which is None wherever the node has no live parent above it.

    Below the aggregates schema there is nothing to re-derive, exactly as
    ``apply`` returns early there.
    """
    if not aggregates.ready(connection):
        return
    plan_id = str(before["plan_id"])
    shift(connection, plan_id, before, parent_id=from_parent, sign=-1, at=at)
    shift(connection, plan_id, after, parent_id=to_parent, sign=1, at=at)
