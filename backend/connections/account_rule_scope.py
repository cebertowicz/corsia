"""Where rules may fire on a connected account's unowned mail."""
from __future__ import annotations

import sqlite3

from connections.app_reach import AppReach


def reach_rule_scope(reach: AppReach, db: sqlite3.Connection) -> frozenset[str | None]:
    """Project ids, and ``None`` for chats outside projects. Only-CORSIA reach includes none."""
    if reach.mode == "selected":
        return frozenset((*reach.project_ids, None)) if reach.outside_projects else reach.project_ids
    if reach.mode == "all":
        return frozenset([*(row["id"] for row in db.execute("SELECT id FROM projects")), None])
    return frozenset()
