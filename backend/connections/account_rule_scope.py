"""Connected-account scope for rules on unowned mail."""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)


def reached_projects(account_id: str, *, db: sqlite3.Connection) -> frozenset[str | None] | None:
    """Project ids and None for outside projects; the whole result is None on failure."""
    from connections.connected_accounts import ConnectedAccountError, _account_reach

    try:
        reach = _account_reach(account_id, db=db)
        if reach.mode == "selected":
            return frozenset((*reach.project_ids, None)) if reach.outside_projects else reach.project_ids
        if reach.mode == "all":
            return frozenset([*(row["id"] for row in db.execute("SELECT id FROM projects")), None])
        return frozenset()
    except (ConnectedAccountError, sqlite3.Error):
        log.exception("Rule scope for a connected account is unreadable")
        return None
