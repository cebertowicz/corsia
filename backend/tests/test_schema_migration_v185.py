"""Schema 185: the Later cut, and when a plan reviews its pile.

The step adds two columns and converts nothing: every existing plan keeps its
behaviour exactly, because a plan with no Later rows and no armed review reads as
it did before. So these tests are the whole rung — it registers at 184, adds both
columns to an existing database, runs twice, and leaves a fresh schema and a
migrated one alike.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS
from infra.migration_184_to_185_plan_later import migrate_184_to_185
from infra.schema import SCHEMA_VERSION
from tests.plan_writer_authorizer import raw_plan_seed

#: One open task on one thread plan: the shape a pre-185 database already holds.
_SEED_PLAN = ("INSERT INTO plans(id,owner_kind,owner_id,state,revision,created_at,updated_at)"
              " VALUES ('p','thread','t','active',1,'t','t')")
_SEED_TASK = (
    "INSERT INTO plan_nodes(id,plan_id,order_key,kind,title,acceptance_criteria,"
    "checkpoint_exception,state,requirements_revision,commitment_id,revision,"
    "created_at,updated_at) VALUES ('n','p',0,'task','T','DOD',0,'pending',1,'n',1,'t','t')"
)


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _columns(db: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in db.execute(f"PRAGMA table_info({table})")
    }


def _below_the_cut(db: sqlite3.Connection) -> None:
    """Rewind one fresh database to what a pre-185 plan table is."""
    db.execute("ALTER TABLE plan_nodes DROP COLUMN later")
    db.execute("ALTER TABLE plans DROP COLUMN later_review_at")
    db.execute("PRAGMA user_version=184")


def test_the_step_is_registered_at_184() -> None:
    assert SCHEMA_VERSION == 189
    assert MIGRATIONS[184] is migrate_184_to_185


def test_the_step_adds_both_columns_a_fresh_schema_has_and_runs_twice() -> None:
    with closing(_fresh()) as fresh, closing(_fresh()) as db:
        _below_the_cut(db)
        assert "later" not in _columns(db, "plan_nodes")
        assert "later_review_at" not in _columns(db, "plans")

        migrate_184_to_185(db)
        migrate_184_to_185(db)

        # A migration appends the column and a fresh schema declares it in place,
        # so the shapes match by name and definition rather than by position.
        assert _columns(db, "plan_nodes") == _columns(fresh, "plan_nodes")
        assert _columns(db, "plans") == _columns(fresh, "plans")


def test_existing_work_starts_above_the_cut_with_no_review_armed() -> None:
    with closing(_fresh()) as db:
        _below_the_cut(db)
        with raw_plan_seed(db):
            db.execute(_SEED_PLAN)
            db.execute(_SEED_TASK)
        db.commit()

        migrate_184_to_185(db)

        node = db.execute("SELECT later FROM plan_nodes WHERE id='n'").fetchone()
        plan = db.execute("SELECT later_review_at FROM plans WHERE id='p'").fetchone()
        assert node[0] == 0
        assert plan[0] is None


def test_the_cut_holds_only_a_flag() -> None:
    """The CHECK is the whole rule: nothing else bounds what `later` may be."""
    with closing(_fresh()) as db:
        _below_the_cut(db)
        with raw_plan_seed(db):
            db.execute(_SEED_PLAN)
            db.execute(_SEED_TASK)
        db.commit()
        migrate_184_to_185(db)

        with pytest.raises(sqlite3.IntegrityError), raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET later=2 WHERE id='n'")


def test_the_step_skips_a_database_without_the_plan_tables() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_184_to_185(db)
