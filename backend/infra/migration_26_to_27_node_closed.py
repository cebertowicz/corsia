"""Schema 26 -> 27: a plan node may end as ``closed``.

Until now every way out of an open node claimed something about the work:
``accepted`` that it was done, ``failed`` that it could not be. A node that is
simply no longer wanted had neither, so plans kept it forever or an owner
recorded a false failure. ``closed`` is that missing outcome, and the column's
CHECK constraint is what refuses it today.

SQLite cannot alter a CHECK, so the table is rebuilt. The rebuild is pure
widening: every state legal at 26 is legal at 27, no column changes, and no
row is rewritten -- so nothing here can fail on existing data, and a database
migrated by this step differs from its backup only in what it will now accept.

Two orderings are load-bearing:

- ``legacy_alter_table=ON`` during the rename, so the foreign keys in
  ``plan_node_dependencies``, ``plan_node_attempts`` and the rest keep pointing
  at ``plan_nodes`` instead of following the rename to the temporary name. The
  runner already holds ``foreign_keys=OFF`` across the whole transaction.
- The revision-bump triggers are recreated *after* the rows are copied. Created
  first, the copy would fire ``bump_plan_revision_node_insert`` once per node
  and hand every plan a revision bump that no one made, invalidating the
  ``expected_revision`` every open caller is holding.

The DDL below is a copy of schema 27's, not an import of the live constant, for
the reason the rest of the ladder states: a step must keep producing the shape
it was written for, or a database migrated last month stops matching one
migrated today. ``test_schema_migration_v27`` builds its schema-26 fixture by
removing ``'closed'`` from that copy, so the fixture differs from the target in
exactly the thing under test, and asserts the migrated table, its indexes and
its triggers match a fresh schema 27.
"""

from __future__ import annotations

import logging
import sqlite3

from infra.migration_errors import PlanMigrationError

log = logging.getLogger(__name__)

_OLD_TABLE = "plan_nodes_v26"

#: ``plan_nodes`` exactly as schema 27 defines it: schema 26's table with
#: ``'closed'`` added to the state CHECK, and nothing else changed.
_PLAN_NODES_27 = """
    CREATE TABLE plan_nodes (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
        parent_id TEXT REFERENCES plan_nodes(id) ON DELETE CASCADE,
        order_key INTEGER NOT NULL CHECK (order_key >= 0),
        kind TEXT NOT NULL CHECK (kind IN ('phase', 'track', 'task')),
        title TEXT NOT NULL CHECK (length(trim(title)) > 0),
        description TEXT,
        acceptance_criteria TEXT NOT NULL
            CHECK (length(trim(acceptance_criteria)) > 0),
        checkpoint_exception INTEGER NOT NULL DEFAULT 0
            CHECK (checkpoint_exception IN (0, 1)),
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN (
                'pending', 'active', 'review_requested', 'accepted',
                'accepted_with_followup', 'failed', 'closed'
            )),
        assignee TEXT,
        spawn_kind TEXT,
        owner_thread_id TEXT REFERENCES threads(id),
        executor_thread_id TEXT REFERENCES threads(id),
        spawned_thread_id TEXT REFERENCES threads(id),
        follow_up_of TEXT REFERENCES plan_nodes(id),
        requirements_revision INTEGER NOT NULL DEFAULT 1
            CHECK (requirements_revision > 0),
        commitment_id TEXT NOT NULL REFERENCES plan_nodes(id),
        watchdog_budget_seconds INTEGER
            CHECK (watchdog_budget_seconds IS NULL OR watchdog_budget_seconds > 0),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    )
"""

_NODE_COLUMNS = (
    "id", "plan_id", "parent_id", "order_key", "kind", "title", "description",
    "acceptance_criteria", "checkpoint_exception", "state", "assignee",
    "spawn_kind", "owner_thread_id", "executor_thread_id", "spawned_thread_id",
    "follow_up_of", "requirements_revision", "commitment_id",
    "watchdog_budget_seconds", "revision", "created_at", "updated_at",
    "archived_at",
)

_SECONDARY = (
    "CREATE INDEX idx_plan_nodes_tree ON plan_nodes(plan_id, parent_id, order_key)",
    "CREATE INDEX idx_plan_nodes_follow_up ON plan_nodes(follow_up_of)",
    "CREATE INDEX idx_plan_nodes_commitment ON plan_nodes(commitment_id, created_at)",
    "CREATE INDEX idx_plan_nodes_spawned_thread ON plan_nodes(spawned_thread_id)",
    "CREATE INDEX idx_plan_nodes_executor_thread ON plan_nodes(executor_thread_id)",
    """
    CREATE TRIGGER bump_plan_revision_node_insert
    AFTER INSERT ON plan_nodes
    BEGIN
        UPDATE plans SET revision=revision+1, updated_at=NEW.updated_at
        WHERE id=NEW.plan_id;
    END
    """,
    """
    CREATE TRIGGER bump_plan_revision_node_update
    AFTER UPDATE ON plan_nodes
    BEGIN
        UPDATE plans SET revision=revision+1, updated_at=NEW.updated_at
        WHERE id=NEW.plan_id;
    END
    """,
    """
    CREATE TRIGGER bump_plan_revision_node_delete
    AFTER DELETE ON plan_nodes
    BEGIN
        UPDATE plans SET revision=revision+1,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE id=OLD.plan_id;
    END
    """,
)


def migrate_26_to_27(conn: sqlite3.Connection) -> None:
    """Widen the ``plan_nodes`` state CHECK to admit ``closed``."""
    _preflight(conn)
    columns = ",".join(f'"{name}"' for name in _NODE_COLUMNS)
    # Legacy rename semantics leave other tables' foreign keys on the original
    # name; modern semantics would follow the rename to the temporary table.
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute(f'ALTER TABLE plan_nodes RENAME TO "{_OLD_TABLE}"')
        _drop_renamed_objects(conn)
        conn.execute(_PLAN_NODES_27)
        # Before the triggers exist: a bump per copied row would invalidate
        # every expected_revision currently in flight.
        conn.execute(
            f'INSERT INTO plan_nodes ({columns}) '
            f'SELECT {columns} FROM "{_OLD_TABLE}" ORDER BY rowid'
        )
        conn.execute(f'DROP TABLE "{_OLD_TABLE}"')
        for statement in _SECONDARY:
            conn.execute(statement)
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
    residue = conn.execute(
        "SELECT name FROM sqlite_master WHERE sql LIKE ? AND sql IS NOT NULL",
        (f"%{_OLD_TABLE}%",),
    ).fetchall()
    if residue:
        raise PlanMigrationError(
            "converted schema still references " + _OLD_TABLE
        )
    log.info("plan_nodes now accepts the closed outcome")


def _preflight(conn: sqlite3.Connection) -> None:
    """Refuse a database this step cannot rebuild, before any effect."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (_OLD_TABLE,),
    ).fetchone():
        raise PlanMigrationError(
            f"database contains a partial v27 conversion ({_OLD_TABLE} exists);"
            " restore the pre-migration backup"
        )
    present = tuple(
        str(row[1]) for row in conn.execute("PRAGMA table_info(plan_nodes)")
    )
    if present != _NODE_COLUMNS:
        raise PlanMigrationError(
            "plan_nodes does not have the schema 26 columns this step copies:"
            f" found {present}"
        )


def _drop_renamed_objects(conn: sqlite3.Connection) -> None:
    """Drop the indexes and triggers the rename carried to the old table."""
    rows = conn.execute(
        """SELECT type,name FROM sqlite_master
           WHERE type IN ('index','trigger') AND tbl_name=? AND sql IS NOT NULL
           ORDER BY CASE type WHEN 'trigger' THEN 0 ELSE 1 END,name""",
        (_OLD_TABLE,),
    ).fetchall()
    for kind, name in rows:
        escaped = str(name).replace('"', '""')
        conn.execute(f'DROP {str(kind).upper()} "{escaped}"')
