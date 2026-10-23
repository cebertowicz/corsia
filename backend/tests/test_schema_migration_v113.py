"""Schema v113 retypes local image uploads without reading their files."""

from __future__ import annotations

import sqlite3

from infra.database_migrations import MIGRATIONS
from infra.migration_112_to_113_retype_upload_images import migrate_112_to_113
from infra.schema import ARTIFACT_IDENTITY_SCHEMA_SQL, SCHEMA_VERSION


def _insert(
    db: sqlite3.Connection,
    *,
    identity_id: str,
    type_: str,
    external_id: str,
    path: str,
) -> None:
    db.execute(
        "INSERT INTO artifact_identities "
        "(id,integration,type,external_id,path,created_at,updated_at) "
        "VALUES (?, 'upload', ?, ?, ?, 't', 't')",
        (identity_id, type_, external_id, path),
    )


def _type_for(db: sqlite3.Connection, identity_id: str) -> str:
    return db.execute(
        "SELECT type FROM artifact_identities WHERE id=?", (identity_id,)
    ).fetchone()[0]


def test_registered_as_the_current_step():
    assert SCHEMA_VERSION == 144
    assert MIGRATIONS[112] is migrate_112_to_113


def test_retypes_only_local_image_uploads_and_is_idempotent():
    db = sqlite3.connect(":memory:")
    db.executescript(ARTIFACT_IDENTITY_SCHEMA_SQL)
    _insert(db, identity_id="local-image", type_="upload", external_id="local-image", path="/tmp/photo.PNG")
    _insert(db, identity_id="url-image", type_="upload", external_id="url-image", path="https://example.test/photo.png")
    _insert(db, identity_id="non-image", type_="upload", external_id="non-image", path="/tmp/archive.zip")
    _insert(db, identity_id="other-integration", type_="upload", external_id="other", path="/tmp/photo.png")
    db.execute("UPDATE artifact_identities SET integration='workspace' WHERE id='other-integration'")

    migrate_112_to_113(db)
    migrate_112_to_113(db)

    assert _type_for(db, "local-image") == "image"
    assert _type_for(db, "url-image") == "upload"
    assert _type_for(db, "non-image") == "upload"
    assert _type_for(db, "other-integration") == "upload"


def test_twin_image_identity_keeps_legacy_upload_unchanged():
    db = sqlite3.connect(":memory:")
    db.executescript(ARTIFACT_IDENTITY_SCHEMA_SQL)
    _insert(db, identity_id="legacy", type_="upload", external_id="same-file", path="/tmp/photo.png")
    _insert(db, identity_id="typed", type_="image", external_id="same-file", path="/tmp/photo.png")

    migrate_112_to_113(db)

    assert _type_for(db, "legacy") == "upload"
    assert _type_for(db, "typed") == "image"


def test_upload_path_index_is_added_and_used_for_canonical_lookup():
    db = sqlite3.connect(":memory:")
    before_index, _separator, _after_index = ARTIFACT_IDENTITY_SCHEMA_SQL.partition(
        "CREATE INDEX IF NOT EXISTS idx_artifact_identities_upload_path"
    )
    db.executescript(before_index)
    _insert(
        db, identity_id="image", type_="image", external_id="image-id",
        path="/workspace/.threads/main/uploads/image-id/photo.png",
    )

    migrate_112_to_113(db)

    plan = db.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT integration, type, external_id FROM artifact_identities "
        "WHERE integration='upload' AND type IN (?, ?) AND path=? "
        "AND state='active' ORDER BY type='image' DESC LIMIT 1",
        ("upload", "image", "/workspace/.threads/main/uploads/image-id/photo.png"),
    ).fetchall()

    assert any("idx_artifact_identities_upload_path" in row[-1] for row in plan)
