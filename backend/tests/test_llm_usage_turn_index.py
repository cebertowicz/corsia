"""The turn index is identical on fresh and migrated ledgers."""

import logging
import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_192_to_193_llm_usage_turn_index import migrate_192_to_193


QUERY = "SELECT 1 FROM llm_usage WHERE turn_id=? AND source=? AND id<>? LIMIT 1"


def test_migration_matches_fresh_schema_and_preserves_queries(caplog):
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='idx_llm_usage_turn'").fetchone()[0]
        db.execute("DROP INDEX idx_llm_usage_turn")
        db.executemany(
            "INSERT INTO llm_usage(id,thread_id,turn_id,model,source,created_at) VALUES (?,?,?,?,?,?)",
            [("a", "thread", "turn", "model", "call", "now"),
             ("b", "thread", None, "model", "call", "now")],
        )
        probes = [("turn", "call", "other"), ("turn", "call", "a"), ("none", "call", "other")]
        before = [db.execute(QUERY, probe).fetchone() for probe in probes]
        with caplog.at_level(logging.INFO, logger="infra.database_migrations"):
            migrate_192_to_193(db)
        assert "indexed_rows=1 build_ms=" in caplog.text
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='idx_llm_usage_turn'").fetchone()[0] == sql
        assert [db.execute(QUERY, probe).fetchone() for probe in probes] == before
        plan = db.execute("EXPLAIN QUERY PLAN " + QUERY, probes[0]).fetchall()
        assert any("SEARCH llm_usage USING INDEX idx_llm_usage_turn" in row[-1] for row in plan)
        migrate_192_to_193(db)


def test_runner_upgrades_192_once():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        db.execute("DROP INDEX idx_llm_usage_turn")
        db.execute("PRAGMA user_version=192")
        db.commit()
        assert MIGRATIONS[192] is migrate_192_to_193
        assert migrate_to_current(db, 193).migrated
        assert db.execute("PRAGMA user_version").fetchone()[0] == 193
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_llm_usage_turn'").fetchone()
        assert not migrate_to_current(db, 193).migrated


def test_migration_skips_missing_table_and_does_not_commit():
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_192_to_193(db)
        db.execute("CREATE TABLE llm_usage(id TEXT, turn_id TEXT)")
        db.execute("BEGIN")
        migrate_192_to_193(db)
        db.rollback()
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_llm_usage_turn'").fetchone() is None
