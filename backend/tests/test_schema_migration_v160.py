"""Schema 159 -> 160: rename persisted untrusted worker confinement."""

import json
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_159_to_160_untrusted_confinement import migrate_159_to_160
from infra.schema import SCHEMA_VERSION


def test_upgrade_renames_modes_and_preserves_other_values():
    db = sqlite3.connect(":memory:")
    fresh = sqlite3.connect(":memory:")
    try:
        initialize_schema(db)
        db.execute("DROP TABLE measure_files")
        db.execute("PRAGMA user_version=159")
        for name, mode in (("old", "review"), ("other", "none"), ("null", None)):
            db.execute(
                "INSERT INTO threads (id,user_id,confinement,created_at,updated_at) VALUES (?,'u',?,'t','t')",
                (name, mode),
            )
        for name, metadata in (
            ("old", '{"claude_invocation":{"confinement":"review","add_dirs":["/src"]}}'),
            ("other", '{"claude_invocation":{"confinement":"none"}}'),
            ("malformed", 'not json'),
        ):
            db.execute(
                "INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at) "
                "VALUES (?,'claude-code','session',?,?,'t','t')", (name, name, metadata),
            )
        db.execute(
            "INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at) "
            "VALUES ('unrelated','other','session','unrelated',?,'t','t')",
            ('{"claude_invocation":{"confinement":"review"}}',),
        )
        db.commit()
        assert SCHEMA_VERSION >= 160 and MIGRATIONS[159] is migrate_159_to_160
        assert migrate_to_current(db, 160).to_version == 160
        assert dict(db.execute("SELECT id,confinement FROM threads")) == {
            "old": "untrusted", "other": "none", "null": None,
        }
        metadata = dict(db.execute("SELECT id,metadata FROM artifact_identities"))
        assert json.loads(metadata["old"]) == {"claude_invocation": {"confinement": "untrusted", "add_dirs": ["/src"]}}
        assert json.loads(metadata["other"]) == {"claude_invocation": {"confinement": "none"}}
        assert metadata["malformed"] == 'not json'
        assert json.loads(metadata["unrelated"]) == {"claude_invocation": {"confinement": "review"}}
        migrate_159_to_160(db)
        assert metadata == dict(db.execute("SELECT id,metadata FROM artifact_identities"))
        initialize_schema(fresh)
        for table in ("threads", "artifact_identities"):
            assert list(db.execute(f"PRAGMA table_info({table})")) == list(fresh.execute(f"PRAGMA table_info({table})"))
        assert not migrate_to_current(db, 160).migrated
    finally:
        db.close()
        fresh.close()
