"""The suggestion-history migration preserves existing data and joins the ladder."""
import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_204_to_205_suggestion_history import migrate_204_to_205
from infra.schema import SCHEMA_VERSION


def test_suggestion_history_migration_is_registered_and_preserves_existing_rows():
    assert SCHEMA_VERSION == 205
    assert MIGRATIONS[204] is migrate_204_to_205
    db = sqlite3.connect(':memory:')
    try:
        db.execute('CREATE TABLE existing (value TEXT NOT NULL)')
        db.execute("INSERT INTO existing VALUES ('kept')")
        migrate_204_to_205(db)
        db.execute(
            "INSERT INTO doc_suggestion_history VALUES (?, ?, ?, ?, ?, ?, ?)",
            ('event', 'artifact', '[]', '{}', 'before', 'after', 'accepted'),
        )
        assert db.execute('SELECT value FROM existing').fetchone()[0] == 'kept'
        assert db.execute('SELECT state FROM doc_suggestion_history').fetchone()[0] == 'accepted'
    finally:
        db.close()
