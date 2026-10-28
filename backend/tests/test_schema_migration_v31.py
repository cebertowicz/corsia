"""Schema v31 indexes plan_node_id on llm_usage and active_turns (#2090)."""

from __future__ import annotations

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION

_NEW_INDEXES = {"idx_llm_usage_plan_node", "idx_active_turns_plan_node"}


def _index_names(db) -> set[str]:
    return {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN "
            "('idx_llm_usage_plan_node','idx_active_turns_plan_node')"
        )
    }


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 157


def test_fresh_schema_creates_the_plan_node_indexes():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert _index_names(db) == _NEW_INDEXES


def test_startup_migration_adds_the_indexes_to_an_existing_database(tmp_path):
    path = tmp_path / "legacy-no-plan-node-index.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP INDEX idx_llm_usage_plan_node")
        db.execute("DROP INDEX idx_active_turns_plan_node")
        db.execute("PRAGMA user_version=30")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 31)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 30, 31,
        )
        assert _index_names(db) == _NEW_INDEXES
        assert not migrate_to_current(db, 31).migrated


def test_the_plan_cost_queries_search_the_new_indexes_instead_of_scanning():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        usage_plan = db.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT plan_node_id,total_tokens,cost_usd FROM llm_usage "
            "WHERE plan_node_id IN ('a','b')"
        ).fetchall()
        turns_plan = db.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT plan_node_id,status,started_at,ended_at FROM active_turns "
            "WHERE plan_node_id IN ('a','b')"
        ).fetchall()
        usage_detail = " ".join(str(row[-1]) for row in usage_plan)
        turns_detail = " ".join(str(row[-1]) for row in turns_plan)
        assert "SEARCH llm_usage USING INDEX idx_llm_usage_plan_node" in usage_detail
        assert "SEARCH active_turns USING INDEX idx_active_turns_plan_node" in turns_detail
        assert "SCAN" not in usage_detail
        assert "SCAN" not in turns_detail
