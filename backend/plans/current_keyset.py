"""The keyset a full ``plan_read`` page continues from (split from ``plans.current``)."""

from __future__ import annotations

import bisect

import plans.open_tree as open_tree
import plans.status_cursor as status_cursor
from plans.errors import PlanError


def sort_keys(tree: open_tree.OpenTree) -> list[tuple]:
    """The sort key of each listed row, in listed order: its siblings' ``(order_key, id)`` down its path.

    A parent's key is a prefix of its children's, so keys ascend in canonical plan
    order. A row the tree cannot reach (stranded, no path) sorts after the rest.
    """
    rows = {str(row["id"]): row for row in tree.rows}
    keys = []
    for row, path in tree.listed:
        pairs = tuple((rows[node_id]["order_key"], node_id) for node_id in (*path, str(row["id"])))
        keys.append((0, *pairs) if path or row["parent_id"] is None else (1, *pairs))
    return keys


def after(position: dict | None, keys: list[tuple]) -> int:
    """Where a continued page starts: the first listed row sorting after the cursor's key."""
    if position is None:
        return 0
    key = position.get("k")
    if key is None:
        raise PlanError(status_cursor.FULL_STALE)
    return bisect.bisect_right(keys, (key[0], *map(tuple, key[1:])))
