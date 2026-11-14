"""Where rules may fire on a connected account's unowned mail."""
from __future__ import annotations

import sqlite3

from connections.app_reach import AppReach
from infra.delegation_scope import scope_label


def reach_rule_scope(reach: AppReach, db: sqlite3.Connection) -> frozenset[str]:
    """Rule scopes reached by selected or all-project account access."""
    if reach.mode == "selected":
        scopes = {scope_label(pid) for pid in reach.project_ids}
        if reach.outside_projects:
            scopes.add(scope_label(None))
        return frozenset(scopes)
    if reach.mode == "all":
        return frozenset({scope_label(row["id"]) for row in db.execute("SELECT id FROM projects")} | {scope_label(None)})
    return frozenset()
