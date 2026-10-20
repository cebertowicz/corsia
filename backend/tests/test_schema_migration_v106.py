"""Schema v106 adds the stored plan aggregates and proves them against a recount (#731)."""

from __future__ import annotations

import sqlite3

import pytest

from infra import migration_105_to_106_plan_aggregates as step
from infra.database_migrations import MIGRATIONS, MigrationError, migrate_to_current
from infra.plan_aggregates_schema import (
    NODE_AGGREGATE_COLUMNS, NODE_AGGREGATE_INDEXES, NODE_AGGREGATE_NAMES,
    PLAN_AGGREGATE_COLUMNS, PRE_AGGREGATE_NODE_COLUMNS,
)
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION
from plans import closure, mutations, rejection, service
from plans.aggregates_recount import stored_mismatches
from tests.schema_rewind import rewind_plan_aggregates

#: The revision trigger as schemas before 106 created it: any node update bumps the plan.
_OLD_TRIGGER = (
    "CREATE TRIGGER bump_plan_revision_node_update AFTER UPDATE ON plan_nodes BEGIN "
    "UPDATE plans SET revision=revision+1, updated_at=NEW.updated_at WHERE id=NEW.plan_id; END"
)


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at)"
        " VALUES ('owner','user','p','Owner','t','t')"
    )
    db.commit()
    return db


def _task(db, plan_id: str, title: str, **kwargs):
    kind = kwargs.pop("kind", "task")
    return service.add_node(
        plan_id, kind=kind, title=title, acceptance_criteria="done", db=db, **kwargs,
    )


def _legacy() -> tuple[sqlite3.Connection, dict[str, str]]:
    """A schema-105 database: a plan with every outcome, a sent-back round, an archived row."""
    db = _fresh()
    plan = service.ensure_plan("thread", "owner", db=db)
    ids: dict[str, str] = {}
    ids["phase"] = _task(db, plan.id, "Phase", kind="phase").id
    for key in ("done", "lost", "dropped", "sent", "open", "gone"):
        ids[key] = _task(db, plan.id, key, parent_id=ids["phase"]).id
    service.transition_node(ids["done"], "active", db=db)
    mutations.request_review(ids["done"], evidence="proven", actor_id="owner", db=db)
    service.transition_node(
        ids["done"], "accepted", verification_result="passed", verified_by="t", db=db,
    )
    service.transition_node(ids["lost"], "active", db=db)
    service.transition_node(ids["lost"], "failed", db=db)
    closure.close_node(ids["dropped"], reason="not wanted", provenance="test", db=db)
    service.transition_node(ids["sent"], "active", db=db)
    mutations.request_review(ids["sent"], evidence="proven", actor_id="owner", db=db)
    _, revision, _ = rejection.reject_submission(ids["sent"], feedback="again", db=db)
    ids["revision"] = revision.id
    from plans import archival

    archival.archive_node(ids["gone"], db=db)
    db.commit()
    rewind_plan_aggregates(db)
    db.execute("DROP TRIGGER bump_plan_revision_node_update")
    db.execute(_OLD_TRIGGER)
    db.execute("PRAGMA user_version=105")
    db.commit()
    return db, ids


def _thread_plan_revision(db) -> int:
    return db.execute("SELECT revision FROM plans WHERE owner_kind='thread'").fetchone()[0]


def _columns(db, table: str) -> dict[str, tuple]:
    return {
        row["name"]: (row["type"], row["notnull"], row["dflt_value"], row["pk"])
        for row in db.execute(f"PRAGMA table_info({table})")
    }


def _objects(db, table: str) -> dict[str, str]:
    return {
        row["name"]: " ".join(row["sql"].split()) for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger')"
            " AND sql IS NOT NULL", (table,),
        )
    }


def test_the_step_is_registered_and_106_is_current() -> None:
    assert SCHEMA_VERSION == 132
    assert MIGRATIONS[105] is step.migrate_105_to_106


def test_a_migrated_database_has_the_shape_of_a_fresh_one() -> None:
    db, _ = _legacy()
    fresh = _fresh()
    assert "sub_open" not in _columns(db, "plan_nodes")

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.migrated, result.from_version, result.to_version) == (True, 105, SCHEMA_VERSION)
    for table in ("plan_nodes", "plans"):
        assert _columns(db, table) == _columns(fresh, table), table
        assert _objects(db, table) == _objects(fresh, table), table
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_every_node_column_is_either_before_the_aggregates_or_one_of_them() -> None:
    columns = {row["name"] for row in _fresh().execute("PRAGMA table_info(plan_nodes)")}
    before = set(PRE_AGGREGATE_NODE_COLUMNS)
    assert not before & NODE_AGGREGATE_NAMES
    assert columns == before | NODE_AGGREGATE_NAMES, columns ^ (before | NODE_AGGREGATE_NAMES)
    assert {name for name, _ in NODE_AGGREGATE_COLUMNS} == NODE_AGGREGATE_NAMES
    assert len(NODE_AGGREGATE_INDEXES) == 6
    assert len(PLAN_AGGREGATE_COLUMNS) == 10


def test_the_migration_computes_aggregates_equal_to_the_recount_and_moves_no_plan() -> None:
    db, ids = _legacy()
    plans_before = [tuple(row) for row in db.execute("SELECT id,revision,updated_at FROM plans")]
    nodes_before = {
        row["id"]: (row["revision"], row["updated_at"]) for row in db.execute("SELECT * FROM plan_nodes")
    }

    migrate_to_current(db, SCHEMA_VERSION)

    assert [tuple(row) for row in db.execute("SELECT id,revision,updated_at FROM plans")] == plans_before
    assert {
        row["id"]: (row["revision"], row["updated_at"]) for row in db.execute("SELECT * FROM plan_nodes")
    } == nodes_before
    for (plan_id,) in db.execute("SELECT id FROM plans").fetchall():
        assert stored_mismatches(db, plan_id) == []
    plan = db.execute("SELECT * FROM plans WHERE owner_kind='thread'").fetchone()
    assert plan["top_roots"] == 1  # the phase; the archived child is not listed
    sent = {row["id"]: row["sent_back"] for row in db.execute("SELECT id,sent_back FROM plan_nodes")}
    assert sent[ids["sent"]] == 1 and sent[ids["dropped"]] == 0 and sent[ids["revision"]] == 0
    listed = {row["id"]: row["listed"] for row in db.execute("SELECT id,listed FROM plan_nodes")}
    assert listed[ids["gone"]] == 0 and listed[ids["open"]] == 1


def test_a_plan_write_after_the_migration_still_bumps_the_plan_but_the_hook_does_not() -> None:
    db, ids = _legacy()
    migrate_to_current(db, SCHEMA_VERSION)
    before = _thread_plan_revision(db)

    service.transition_node(ids["open"], "active", db=db)
    after = _thread_plan_revision(db)

    # One node write, one plan bump; the aggregate UPDATEs on the ancestors added none.
    assert after == before + 1


def test_the_plans_service_runs_before_the_columns_exist() -> None:
    """Older rungs of the ladder call the plans service mid-migration, so it must not need them."""
    db, ids = _legacy()
    assert "sub_open" not in _columns(db, "plan_nodes")

    service.transition_node(ids["open"], "active", db=db)
    added = _task(db, service.ensure_plan("thread", "owner", db=db).id, "Late")

    assert service._node_row(db, ids["open"])["state"] == "active"
    assert added.id


def test_a_recount_mismatch_fails_the_whole_migration_and_changes_nothing(monkeypatch) -> None:
    db, _ = _legacy()
    from plans import public

    monkeypatch.setattr(public, "plan_aggregate_mismatches", lambda conn, plan_id: ["sub_open:x"])

    with pytest.raises(MigrationError, match="differ from the recount"):
        migrate_to_current(db, SCHEMA_VERSION)

    assert db.execute("PRAGMA user_version").fetchone()[0] == 105
    assert "sub_open" not in _columns(db, "plan_nodes")
    assert _objects(db, "plan_nodes").get("bump_plan_revision_node_update") == " ".join(_OLD_TRIGGER.split())
