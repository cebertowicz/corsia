"""What a pane read folds, per level, from the stored counters (#731 S2-2).

The server is the only fold authority: a read that lists the open work and a window of
folded rows (``open_tree.FoldWindow``) says, for every level it lists, how many folded
rows sit directly beneath it and how many of them it left out. The client computes no
fold, held row or count of its own.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from plans import open_tree

TONES = ("done", "failed", "closed")


def levels(tree: open_tree.OpenTree) -> list[dict[str, Any]]:
    """The plan's top level and each listed row that has folded rows directly beneath it.

    ``done``, ``failed`` and ``closed`` are every folded row of the level by tone, as the
    stored counters hold them (a sent-back round is no row of the pane, so it is never
    counted); ``more`` is how many of those the read did not list, which the folded-history
    route pages through. A level with none folded is left out.
    """
    listed = Counter(
        None if row["parent_id"] is None else str(row["parent_id"])
        for row in tree.rows
        if row["folded"] and not row["sent_back"]
    )
    sources: list[tuple[str | None, Any, str]] = [(None, tree.plan, "top_folded")]
    sources += [(str(row["id"]), row, "child_folded") for row in tree.rows]
    found: list[dict[str, Any]] = []
    for parent, source, prefix in sources:
        counts = {tone: int(source[f"{prefix}_{tone}"]) for tone in TONES}
        total = sum(counts.values())
        if total:
            found.append({"parent_id": parent, **counts, "more": total - listed[parent]})
    return found
