"""Schema 103 -> 104: workspace image files registered as ``code`` become ``image``."""
from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_103_to_104_retype_legacy_images import migrate_103_to_104
from infra.schema import SCHEMA_VERSION


def _identity(db, identity_id, *, type_, external_id, path, integration="workspace"):
    db.execute(
        "INSERT INTO artifact_identities(id, integration, type, external_id, path, title, description, "
        "state, retention, metadata, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'title', 'desc', 'active', 'durable', '{\"k\": 1}', 'c', 'u')",
        (identity_id, integration, type_, external_id, path),
    )


def _rows(db):
    return {r[0]: r[1:] for r in db.execute("SELECT id, type, title, description, state, retention, metadata, created_at, updated_at, path FROM artifact_identities")}


def _v103_database(path):
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _identity(db, "png", type_="code", external_id="/ws/a.png", path="/ws/a.png")
        _identity(db, "upper", type_="code", external_id="/ws/B.JPEG", path="/ws/B.JPEG")
        _identity(db, "logical", type_="code", external_id="workspace:shots/c.webp", path=None)
        _identity(db, "svg", type_="code", external_id="/ws/d.svg", path="/ws/d.svg")
        _identity(db, "py", type_="code", external_id="/ws/main.py", path="/ws/main.py")
        _identity(db, "noext", type_="code", external_id="/ws/png", path="/ws/png")
        _identity(db, "doc", type_="document", external_id="/ws/n.md", path="/ws/n.md")
        _identity(db, "already", type_="image", external_id="/ws/e.png", path="/ws/e.png")
        _identity(db, "repo", type_="code", external_id="/repo/f.png", path="/repo/f.png", integration="github")
        # A file with both a legacy code row and an image row: only one may hold the key.
        _identity(db, "twin-code", type_="code", external_id="/ws/g.png", path="/ws/g.png")
        _identity(db, "twin-image", type_="image", external_id="/ws/g.png", path="/ws/g.png")
        db.execute("PRAGMA user_version=103")
        db.commit()


def test_migration_is_registered():
    assert SCHEMA_VERSION == 135 and MIGRATIONS[103] is migrate_103_to_104


def test_legacy_image_rows_are_retyped_and_only_their_type_changes(tmp_path):
    path = tmp_path / "v103.db"
    _v103_database(path)
    with closing(sqlite3.connect(path)) as db:
        before = _rows(db)
        migrate_to_current(db, SCHEMA_VERSION)
        after = _rows(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 135

    retyped = {"png", "upper", "logical", "svg"}
    for identity_id in retyped:
        assert after[identity_id][0] == "image"
    # Left alone: other extensions, other types, other integrations, and a twin that holds the key.
    for identity_id in set(before) - retyped:
        assert after[identity_id] == before[identity_id], identity_id
    # Nothing but `type` moved on a retyped row.
    for identity_id in retyped:
        assert after[identity_id][1:] == before[identity_id][1:]


def test_second_run_changes_nothing(tmp_path):
    path = tmp_path / "v103.db"
    _v103_database(path)
    with closing(sqlite3.connect(path)) as db:
        migrate_to_current(db, SCHEMA_VERSION)
        settled = _rows(db)
        migrate_103_to_104(db)  # the step itself is idempotent, not only the runner
        assert _rows(db) == settled
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_database_without_an_identity_table_has_nothing_to_retype():
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_103_to_104(db)
        assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
