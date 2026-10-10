"""Schema 23 -> 24: name the retirement stamp for what it now records.

``node_retired_at`` was named for its only cause at the time: a plan node
settling. Schema 23 gave the same column a second cause, an artifact's own
end, without renaming it. A column named for one cause that also holds facts
from another misleads every future reader of pin, unpin, and re-registration
code, so this is a pure rename with no data change.
"""

from __future__ import annotations

import sqlite3


def migrate_23_to_24(conn: sqlite3.Connection) -> None:
    """Rename artifact_registrations.node_retired_at to retired_at."""
    conn.execute(
        "ALTER TABLE artifact_registrations RENAME COLUMN node_retired_at TO retired_at"
    )
