"""Schema step 7 -> 8: executor-bound plan admission.

Rebuilds ``plan_nodes``, ``active_turns`` and ``llm_usage`` to the current
shape, creates ``plan_node_attempts``, ``plan_tool_dispatches`` and
``plan_node_holds``, and converts the data those tables need: one commitment
id per lineage, one selected attempt per plan/executor where the choice is
unambiguous, legacy review requests reset to active (they predate acceptance
evidence), and the generic blocked-node attention artifacts retired.

The runner in ``infra.database_migrations`` owns the transaction, the backup,
``user_version`` and post-validation. This step only moves schema and data,
and raises ``PlanMigrationError`` rather than guessing when the source cannot
be converted without data loss.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from infra.migration_7_to_8_lineage import commitments
from infra.migration_errors import PlanMigrationError

_PROVENANCE = "schema_v8_migration"
_REBUILT_TABLES = ("plan_nodes", "active_turns", "llm_usage")
_NEW_TABLES = ("plan_node_attempts", "plan_tool_dispatches", "plan_node_holds")
_TARGET_TABLES = (*_REBUILT_TABLES, *_NEW_TABLES)
_NEW_NODE_COLUMNS = {"executor_thread_id", "requirements_revision", "commitment_id"}
_NODE_COLUMNS = (
    "id", "plan_id", "parent_id", "order_key", "kind", "title", "description",
    "acceptance_criteria", "checkpoint_exception", "state", "assignee",
    "spawn_kind", "owner_thread_id", "executor_thread_id", "spawned_thread_id",
    "follow_up_of", "requirements_revision", "commitment_id",
    "watchdog_budget_seconds", "revision", "created_at", "updated_at",
    "archived_at",
)




def migrate_7_to_8(conn: sqlite3.Connection) -> None:
    """Rebuild the plan admission tables and convert their data in place."""
    conn.row_factory = sqlite3.Row
    _preflight(conn)
    migrated_at = _now(conn)
    resolved = commitments(conn)
    desired_tables, secondary = _desired_objects()
    # Renaming must leave other tables' foreign keys pointing at the original
    # name, which only legacy rename semantics guarantee; the runner already
    # has foreign_keys off for the whole transaction.
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        for table in _REBUILT_TABLES:
            conn.execute(f'ALTER TABLE "{table}" RENAME TO "{table}_v7"')
        _drop_renamed_objects(conn)
        for table in _TARGET_TABLES:
            conn.execute(desired_tables[table])
        review_nodes = _copy_plan_nodes(conn, resolved, migrated_at)
        _copy_shared_columns(conn, "active_turns", "active_turns_v7")
        _copy_shared_columns(conn, "llm_usage", "llm_usage_v7")
        for table in _REBUILT_TABLES:
            conn.execute(f'DROP TABLE "{table}_v7"')
        for statement in secondary:
            conn.execute(statement)
        _reset_legacy_reviews(conn, review_nodes, migrated_at)
        _backfill_attempts(conn, migrated_at)
        _retire_generic_node_attention(conn, migrated_at)
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
    residue = conn.execute(
        "SELECT name FROM sqlite_master WHERE sql LIKE '%_v7%' AND sql IS NOT NULL"
    ).fetchall()
    if residue:
        raise PlanMigrationError("converted schema references temporary v7 tables")


def _now(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0])


def _tables(db: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(db: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")'))


def _preflight(db: sqlite3.Connection) -> None:
    tables = _tables(db)
    required = {
        "threads", "plans", "plan_nodes", "plan_node_events",
        "plan_node_dependencies", "plan_node_artifact_links",
        "artifact_identities", "expected_event_watches", "active_turns", "llm_usage",
    }
    missing = sorted(required - tables)
    if missing:
        raise PlanMigrationError("schema v7 is missing: " + ", ".join(missing))
    if set(_NEW_TABLES) & tables or _NEW_NODE_COLUMNS & set(_columns(db, "plan_nodes")):
        raise PlanMigrationError(
            "database contains a partial v8 conversion; restore the v7 backup"
        )


def _desired_objects() -> tuple[dict[str, str], tuple[str, ...]]:
    """Read the exact current DDL for the target tables from a fresh schema.

    This step is the newest rung, so the live schema is the shape it must
    produce; ``test_schema_migration_v8`` asserts the migrated database matches
    a fresh one, which is what catches a later schema edit made without a step.
    """
    from infra.schema import SCHEMA_SQL

    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(SCHEMA_SQL)
        tables = {}
        for name in _TARGET_TABLES:
            row = reference.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            if row is None or not row[0]:
                raise PlanMigrationError(f"current schema is missing {name}")
            tables[name] = str(row[0])
        placeholders = ",".join("?" for _ in _TARGET_TABLES)
        secondary = tuple(
            str(row[0])
            for row in reference.execute(
                f"""SELECT sql FROM sqlite_master
                    WHERE type IN ('index','trigger')
                      AND tbl_name IN ({placeholders}) AND sql IS NOT NULL
                    ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END,name""",
                _TARGET_TABLES,
            )
        )
        return tables, secondary
    finally:
        reference.close()


def _drop_renamed_objects(db: sqlite3.Connection) -> None:
    old_names = tuple(f"{name}_v7" for name in _REBUILT_TABLES)
    placeholders = ",".join("?" for _ in old_names)
    rows = db.execute(
        f"""SELECT type,name FROM sqlite_master
            WHERE type IN ('index','trigger') AND tbl_name IN ({placeholders})
              AND sql IS NOT NULL
            ORDER BY CASE type WHEN 'trigger' THEN 0 ELSE 1 END,name""",
        old_names,
    ).fetchall()
    for row in rows:
        kind = str(row["type"]).upper()
        name = str(row["name"]).replace('"', '""')
        db.execute(f'DROP {kind} "{name}"')


def _copy_shared_columns(db: sqlite3.Connection, table: str, old_table: str) -> None:
    desired = set(_columns(db, table))
    shared = tuple(name for name in _columns(db, old_table) if name in desired)
    names = ",".join(f'"{name}"' for name in shared)
    db.execute(f'INSERT INTO "{table}" ({names}) SELECT {names} FROM "{old_table}"')
def _copy_plan_nodes(
    db: sqlite3.Connection,
    resolved: dict[str, str],
    migrated_at: str,
) -> tuple[str, ...]:
    """Copy every legacy node and return review requests reset to active."""
    review_nodes = []
    placeholders = ",".join("?" for _ in _NODE_COLUMNS)
    sql = (
        "INSERT INTO plan_nodes (" + ",".join(_NODE_COLUMNS) + ") "
        f"VALUES ({placeholders})"
    )
    for row in db.execute("SELECT * FROM plan_nodes_v7 ORDER BY rowid"):
        review_reset = row["state"] == "review_requested"
        node_id = str(row["id"])
        if review_reset:
            review_nodes.append(node_id)
        db.execute(
            sql,
            (
                row["id"], row["plan_id"], row["parent_id"], row["order_key"],
                row["kind"], row["title"], row["description"],
                row["acceptance_criteria"], row["checkpoint_exception"],
                "active" if review_reset else row["state"], row["assignee"],
                row["spawn_kind"], row["owner_thread_id"], None,
                row["spawned_thread_id"], row["follow_up_of"], 1,
                resolved[node_id], row["watchdog_budget_seconds"],
                int(row["revision"]) + int(review_reset), row["created_at"],
                migrated_at if review_reset else row["updated_at"],
                row["archived_at"],
            ),
        )
    return tuple(review_nodes)


def _reset_legacy_reviews(
    db: sqlite3.Connection,
    node_ids: tuple[str, ...],
    migrated_at: str,
) -> None:
    """Record why legacy review requests cannot cross the evidence cutover."""
    affected_plans = set()
    for node_id in node_ids:
        row = db.execute("SELECT plan_id FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
        affected_plans.add(str(row["plan_id"]))
        db.execute(
            """INSERT INTO plan_node_events
               (id,node_id,actor_id,provenance,event,prior_state,new_state,
                verification_result,causal_event_id,payload_json,created_at)
               VALUES (?,?,NULL,?,'state_transition',
                       'review_requested','active',NULL,NULL,?,?)""",
            (
                f"schema-v8-review-reset:{node_id}",
                node_id,
                _PROVENANCE,
                json.dumps(
                    {"reason": "legacy review request lacks v8 acceptance evidence"},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                migrated_at,
            ),
        )
    for plan_id in affected_plans:
        db.execute(
            "UPDATE plans SET revision=revision+1,updated_at=? WHERE id=?",
            (migrated_at, plan_id),
        )


def _backfill_attempts(db: sqlite3.Connection, migrated_at: str) -> int:
    """Select only the sole eligible active task for each plan/executor."""
    candidates = db.execute(
        """SELECT n.id,n.plan_id,
                  COALESCE(
                    n.executor_thread_id,n.owner_thread_id,
                    CASE WHEN p.owner_kind='thread' THEN p.owner_id END
                  ) AS executor_thread_id
           FROM plan_nodes n JOIN plans p ON p.id=n.plan_id
           WHERE n.kind='task' AND n.state='active' AND n.archived_at IS NULL
             AND COALESCE(n.assignee,'') <> 'user' AND p.state='active'
             AND NOT EXISTS (
               SELECT 1 FROM plan_nodes child
               WHERE child.parent_id=n.id AND child.archived_at IS NULL
             )
             AND NOT EXISTS (
               SELECT 1 FROM plan_node_dependencies d
               JOIN plan_nodes required ON required.id=d.depends_on_node_id
               WHERE d.node_id=n.id
                 AND required.state NOT IN ('accepted','accepted_with_followup')
             )
             AND NOT EXISTS (
               SELECT 1 FROM expected_event_watches w
               WHERE w.plan_node_id=n.id AND w.status='armed'
             )
           ORDER BY n.plan_id,executor_thread_id,n.order_key,n.id"""
    ).fetchall()
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in candidates:
        if row["executor_thread_id"] is not None:
            key = (str(row["plan_id"]), str(row["executor_thread_id"]))
            groups.setdefault(key, []).append(row)
    selected = 0
    for rows in groups.values():
        if len(rows) != 1:
            continue
        row = rows[0]
        node_id = str(row["id"])
        attempt_id = hashlib.sha256(f"schema-v8-attempt:{node_id}".encode()).hexdigest()
        db.execute(
            """INSERT INTO plan_node_attempts
               (id,plan_id,node_id,executor_thread_id,requirements_revision,
                attempt_ordinal,selected_by_actor_id,provenance,selected_at)
               VALUES (?,?,?,?,1,1,NULL,?,?)""",
            (
                attempt_id, row["plan_id"], node_id,
                row["executor_thread_id"], _PROVENANCE, migrated_at,
            ),
        )
        selected += 1
    return selected


def _retire_generic_node_attention(db: sqlite3.Connection, migrated_at: str) -> int:
    """Archive the v7 blocked-node artifacts that falsely required user action."""
    rows = db.execute(
        """SELECT DISTINCT i.id,i.metadata
           FROM artifact_identities i
           JOIN plan_node_artifact_links l ON l.artifact_id=i.id
           WHERE i.integration='local' AND i.type='plan-node'
             AND i.state='active' AND l.role='attention'"""
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update(
            {
                "attention_state": "cleared",
                "attention_outcome": "supersede",
                "attention_reason": "schema_v8_canonical_plan_projection",
            }
        )
        db.execute(
            """UPDATE artifact_identities
               SET state='archived',metadata=?,updated_at=? WHERE id=?""",
            (
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                migrated_at,
                row["id"],
            ),
        )
    return len(rows)
