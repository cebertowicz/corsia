"""Schema v23 retires rows of pull requests that ended before their end retired them."""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION

# artifact id -> (integration, type, pr_state, surfacing, relationship_state, retired)
_ROWS = {
    "merged-visible": ("github", "pull-request", "merged", "visible", "owner-active", None),
    "closed-visible": ("github", "pull-request", "closed", "visible", "owner-active", None),
    "merged-pinned": ("github", "pull-request", "merged", "visible", "owner-pinned", None),
    "merged-quiet": ("github", "pull-request", "merged", "quiet", "owner-active", None),
    "merged-stamped": ("github", "pull-request", "merged", "settled", "owner-active", "earlier"),
    "open-visible": ("github", "pull-request", None, "visible", "owner-active", None),
    "merged-issue": ("github", "issue", "merged", "visible", "owner-active", None),
}


def _legacy_database(tmp_path):
    path = tmp_path / "ended-pull-requests.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        # This migration predates the v23->v24 rename, so its own fixture
        # must reflect the column name that existed at schema v22.
        db.execute(
            "ALTER TABLE artifact_registrations RENAME COLUMN retired_at TO node_retired_at"
        )
        db.execute(
            "INSERT INTO threads(id,user_id,title,created_at,updated_at) "
            "VALUES('thread','owner','Thread','t','t')"
        )
        for artifact_id, (integration, type_, pr_state, surfacing, state, retired) in _ROWS.items():
            metadata = json.dumps({"pr_state": pr_state}) if pr_state else None
            db.execute(
                """INSERT INTO artifact_identities
                   (id,integration,type,external_id,metadata,created_at,updated_at)
                   VALUES(?,?,?,?,?,'t','t')""",
                (artifact_id, integration, type_, f"o/r#{artifact_id}", metadata),
            )
            db.execute(
                """INSERT INTO artifact_registrations
                   (id,artifact_id,parent_thread_id,relationship_state,surfacing,
                    node_retired_at,created_at,updated_at)
                   VALUES(?,?,'thread',?,?,?,'t','t')""",
                (f"registration-{artifact_id}", artifact_id, state, surfacing, retired),
            )
        db.execute("PRAGMA user_version=22")
        db.commit()
    return path


def _registrations(db):
    return {
        artifact_id: (surfacing, retired is not None, retired == "earlier")
        for artifact_id, surfacing, retired in db.execute(
            "SELECT artifact_id,surfacing,node_retired_at FROM artifact_registrations"
        )
    }


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 159


def test_migration_retires_only_rows_of_ended_pull_requests(tmp_path):
    path = _legacy_database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 23)
        assert (result.migrated, result.from_version, result.to_version) == (True, 22, 23)
        after_first = _registrations(db)
        assert after_first == {
            # (surfacing, stamped, kept the earlier stamp)
            "merged-visible": ("settled", True, False),
            "closed-visible": ("settled", True, False),
            "merged-pinned": ("visible", True, False),
            "merged-quiet": ("quiet", True, False),
            "merged-stamped": ("settled", True, True),
            "open-visible": ("visible", False, False),
            "merged-issue": ("visible", False, False),
        }

        assert not migrate_to_current(db, 23).migrated
        assert _registrations(db) == after_first
