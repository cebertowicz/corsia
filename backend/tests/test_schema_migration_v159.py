"""Schema 158 -> 159: remove erroneous confinement from roots and parents."""

import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_158_to_159_unconfine_main_agents import migrate_158_to_159
from infra.schema import SCHEMA_VERSION


def test_upgrade_repairs_roots_and_parents_but_preserves_review_leaf():
    db = sqlite3.connect(":memory:")
    fresh = sqlite3.connect(":memory:")
    try:
        initialize_schema(db)
        db.execute("ALTER TABLE threads DROP COLUMN confinement")
        db.execute("ALTER TABLE threads DROP COLUMN confined_read_dirs")
        db.execute("PRAGMA user_version=154")
        db.commit()
        assert migrate_to_current(db, 158).to_version == 158
        for thread, parent, mode, dirs in (
            ("root", None, "review", '["/root"]'),
            ("parent", "root", "review", '["/parent"]'),
            ("leaf", "parent", "review", '[ "/leaf" ]'),
            ("damaged", None, "review", "[]"),
        ):
            db.execute(
                "INSERT INTO threads (id,user_id,spawned_by,confinement,confined_read_dirs,created_at,updated_at) "
                "VALUES (?,'u',?,?,?,'t','t')", (thread, parent, mode, dirs)
            )
        db.commit()
        assert SCHEMA_VERSION == 165
        assert MIGRATIONS[158] is migrate_158_to_159
        assert migrate_to_current(db, SCHEMA_VERSION).to_version == 165
        rows = {row[0]: tuple(row[1:]) for row in db.execute(
            "SELECT id,confinement,confined_read_dirs FROM threads")}
        assert rows == {"root": (None, None), "parent": (None, None),
                        "leaf": ("untrusted", '[ "/leaf" ]'), "damaged": (None, None)}
        initialize_schema(fresh)
        assert sorted(tuple(row[1:]) for row in db.execute("PRAGMA table_info(threads)")) == sorted(
            tuple(row[1:]) for row in fresh.execute("PRAGMA table_info(threads)"))
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
    finally:
        db.close()
        fresh.close()


def test_missing_table_or_columns_is_a_noop():
    with sqlite3.connect(":memory:") as db:
        migrate_158_to_159(db)
        db.execute("CREATE TABLE threads (id TEXT)")
        migrate_158_to_159(db)
