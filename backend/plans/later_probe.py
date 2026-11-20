"""The schema probe for the Later cut, split from ``plans.node_states`` for file size.

``node_states`` re-exports ``later_ready``, so every reader keeps asking it there. Like
``node_states``, this imports nothing from ``plans``.
"""

from __future__ import annotations

import sqlite3
import weakref

#: Per connection, the schema version at which ``plan_nodes.later`` was last seen.
_LATER_READY: weakref.WeakKeyDictionary[sqlite3.Connection, int] = weakref.WeakKeyDictionary()


def later_ready(connection: sqlite3.Connection) -> bool:
    """Whether ``plan_nodes`` carries the Later cut (schema 185); older ladder rungs run plans code first.

    The answer is kept per connection until its schema changes (``PRAGMA schema_version``),
    exactly as ``plans.aggregates.ready`` keeps it for the aggregate columns. A read that
    has no Later rows to exclude is what a pre-185 database already means, so a caller
    without the column reads the pre-cut behaviour, never an error.
    """
    version = connection.execute("PRAGMA schema_version").fetchone()[0]
    if _LATER_READY.get(connection) == version:
        return True
    found = any(row[1] == "later" for row in connection.execute("PRAGMA table_info(plan_nodes)").fetchall())
    if found:
        _LATER_READY[connection] = version
    return found
