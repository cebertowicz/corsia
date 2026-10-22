"""Schema v27 lets a plan node end as closed, and rewrites nothing else."""

from __future__ import annotations

import sqlite3

import pytest

from infra.migration_26_to_27_node_closed import _PLAN_NODES_27, _SECONDARY
from tests.plan_writer_authorizer import raw_plan_seed


#: Schema 26's ``plan_nodes``: the v27 copy without the one state it added, so
#: the fixture differs from the target in exactly the thing under test.
_PLAN_NODES_26 = _PLAN_NODES_27.replace(", 'closed'", "")


def _object_sql(db: sqlite3.Connection, name: str) -> str:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", (name,)
    ).fetchone()
    assert row is not None, name
    return " ".join(str(row[0]).split())


def _plan_nodes_objects(db: sqlite3.Connection) -> dict[str, str]:
    return {
        str(name): " ".join(str(sql).split())
        for name, sql in db.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE tbl_name='plan_nodes' AND sql IS NOT NULL"
        )
    }


def _schema_twenty_two_database() -> sqlite3.Connection:
    """A populated database whose plan_nodes still refuses ``closed``."""
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    # Swap the current table for schema 26's, carrying its indexes and
    # triggers, so the fixture is a real v26 database rather than a v27 one.
    db.execute("PRAGMA legacy_alter_table=ON")
    for name in _plan_nodes_objects(db):
        if name != "plan_nodes":
            kind = "TRIGGER" if name.startswith("bump_") else "INDEX"
            db.execute(f"DROP {kind} {name}")
    with raw_plan_seed(db):  # dropping the table deletes its rows
        db.execute("DROP TABLE plan_nodes")
    db.execute(_PLAN_NODES_26)
    for statement in _SECONDARY:
        db.execute(statement)
    db.execute("PRAGMA legacy_alter_table=OFF")
    db.execute("PRAGMA user_version=26")
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,execution_epoch,created_at,updated_at) "
        "VALUES ('thread-1','user-1','codex/luna',1,'t0','t0')"
    )
    db.execute(
        "INSERT INTO plans (id,owner_kind,owner_id,state,revision,"
        "created_at,updated_at) "
        "VALUES ('plan-1','thread','thread-1','active',9,'t0','t0')"
    )
    with raw_plan_seed(db):
        for node_id, title in (("node-1", "Kept"), ("node-2", "Dropped")):
            db.execute(
                "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,"
                "acceptance_criteria,state,commitment_id,revision,"
                "created_at,updated_at) "
                "VALUES (?,'plan-1',0,'task',?,'proof','active',?,3,'t0','t0')",
                (node_id, title, node_id),
            )
    db.execute(
        "INSERT INTO plan_node_dependencies "
        "(node_id,depends_on_node_id,created_at) "
        "VALUES ('node-2','node-1','t0')"
    )
    db.execute("UPDATE plans SET revision=9 WHERE id='plan-1'")
    db.commit()
    return db


def test_schema_twenty_two_refuses_the_closed_outcome() -> None:
    """The constraint this step exists to widen is really there beforehand."""
    db = _schema_twenty_two_database()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with raw_plan_seed(db):
                db.execute("UPDATE plan_nodes SET state='closed' WHERE id='node-2'")
    finally:
        db.close()


def test_migrating_admits_closed_and_preserves_every_row() -> None:
    from infra.database_migrations import migrate_to_current
    from infra.schema import SCHEMA_SQL, SCHEMA_VERSION

    fresh = sqlite3.connect(":memory:")
    migrated = _schema_twenty_two_database()
    try:
        fresh.executescript(SCHEMA_SQL)
        result = migrate_to_current(migrated, 27)

        assert SCHEMA_VERSION == 138
        assert result.migrated is True
        assert (result.from_version, result.to_version) == (26, 27)

        # The indexes and triggers match a fresh schema, except the objects schema 101
        # added or narrowed (#731), and the schema-134 intent index adds, which a
        # migration to 27 cannot carry. The table itself gained later workflow fields,
        # so this v27 regression checks its own state transition without pinning later
        # columns out of the current schema.
        from infra.plan_aggregates_schema import NODE_AGGREGATE_INDEXES, TOUCH_INDEXES

        skipped = {
            "plan_nodes", "bump_plan_revision_node_update", "idx_plan_nodes_intent_node",
            *NODE_AGGREGATE_INDEXES, *TOUCH_INDEXES,
        }
        fresh_objects = _plan_nodes_objects(fresh)
        migrated_objects = _plan_nodes_objects(migrated)
        assert {
            key: value for key, value in migrated_objects.items() if key not in skipped
        } == {
            key: value for key, value in fresh_objects.items() if key not in skipped
        }
        assert "'closed'" in _object_sql(migrated, "plan_nodes")

        # Every row survives, and the copy did not fire the revision triggers:
        # a bump here would invalidate every expected_revision in flight.
        assert [
            (row["id"], row["title"], row["state"], row["revision"])
            for row in migrated.execute("SELECT * FROM plan_nodes ORDER BY id")
        ] == [("node-1", "Kept", "active", 3), ("node-2", "Dropped", "active", 3)]
        assert migrated.execute(
            "SELECT revision FROM plans WHERE id='plan-1'"
        ).fetchone()["revision"] == 9

        # The dependency's foreign key still names plan_nodes, not the
        # temporary table the rebuild renamed through.
        assert tuple(migrated.execute(
            "SELECT node_id,depends_on_node_id FROM plan_node_dependencies"
        ).fetchone()) == ("node-2", "node-1")
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "plan_nodes_v26" not in _object_sql(
            migrated, "plan_node_dependencies"
        )

        # The outcome the step exists for is now accepted.
        with raw_plan_seed(migrated):
            migrated.execute("UPDATE plan_nodes SET state='closed' WHERE id='node-2'")
        assert migrated.execute(
            "SELECT state FROM plan_nodes WHERE id='node-2'"
        ).fetchone()["state"] == "closed"
        with pytest.raises(sqlite3.IntegrityError):
            with raw_plan_seed(migrated):
                migrated.execute("UPDATE plan_nodes SET state='abandoned' WHERE id='node-1'")

        repeated = migrate_to_current(migrated, 27)
        assert repeated.migrated is False
        assert (repeated.from_version, repeated.to_version) == (27, 27)
    finally:
        fresh.close()
        migrated.close()


def test_a_partial_conversion_is_refused_rather_than_guessed_at() -> None:
    from infra.migration_26_to_27_node_closed import migrate_26_to_27
    from infra.migration_errors import PlanMigrationError

    db = _schema_twenty_two_database()
    try:
        db.execute("CREATE TABLE plan_nodes_v26 (id TEXT)")
        with pytest.raises(PlanMigrationError, match="partial v27 conversion"):
            migrate_26_to_27(db)
    finally:
        db.close()
