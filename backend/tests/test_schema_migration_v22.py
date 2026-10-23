"""Schema v22 repairs durable workspace registrations retired by plan settlement."""

from __future__ import annotations

from contextlib import closing
import json
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def _legacy_database(tmp_path):
    path = tmp_path / "retired-artifacts.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        # This migration predates the v23->v24 rename, so its own fixture
        # must reflect the column name that existed at schema v21.
        db.execute(
            "ALTER TABLE artifact_registrations RENAME COLUMN retired_at TO node_retired_at"
        )
        db.execute(
            "INSERT INTO threads(id,user_id,title,created_at,updated_at) "
            "VALUES('thread','owner','Thread','t','t')"
        )
        for artifact_id, retention, zone, surfacing, relationship_state in (
            ("durable-visible", "durable", "workspace", "settled", "owner-active"),
            ("durable-quiet", "durable", "workspace", "quiet", "owner-active"),
            ("manually-hidden", "durable", "workspace", "quiet", "owner-active"),
            ("ephemeral", "ephemeral", "workspace", "settled", "owner-active"),
            # Retired while unpinned, then pinned afterward -- the exact
            # sequence a round-4 review found this migration losing by
            # nulling node_retired_at for any currently-pinned row.
            ("pinned-non-exempt", "ephemeral", "workspace", "visible", "owner-pinned"),
        ):
            db.execute(
                """INSERT INTO artifact_identities
                   (id,integration,type,external_id,retention,metadata,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    artifact_id,
                    "workspace",
                    "document",
                    artifact_id,
                    retention,
                    json.dumps({"zone": zone}),
                    "t",
                    "t",
                ),
            )
            db.execute(
                """INSERT INTO artifact_registrations
                   (id,artifact_id,parent_thread_id,relationship_state,surfacing,
                    origin_plan_node_id,node_retired_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,'node','retired','t','t')""",
                (f"registration-{artifact_id}", artifact_id, "thread", relationship_state, surfacing),
            )
            db.execute(
                """INSERT INTO artifact_registration_events
                   (id,artifact_id,event_type,affected_thread_id,
                    old_relationship_state,new_relationship_state,metadata,created_at)
                   VALUES(?,?,'origin_node_retired','thread',
                    'owner-active','owner-active',?,'t')""",
                (
                    f"retired-{artifact_id}",
                    artifact_id,
                    json.dumps(
                        {
                            "old_surfacing": (
                                "quiet" if artifact_id == "durable-quiet" else "visible"
                            )
                        }
                    ),
                ),
            )
        db.execute(
            """INSERT INTO artifact_registration_events
               (id,artifact_id,event_type,affected_thread_id,
                old_relationship_state,new_relationship_state,metadata,created_at)
               VALUES('hidden-after-retirement','manually-hidden','surfacing_changed',
                'thread','settled','quiet',?,'t')""",
            (json.dumps({"old_surfacing": "settled", "new_surfacing": "quiet"}),),
        )
        db.execute("PRAGMA user_version=21")
        db.commit()
    return path


def _registrations(db):
    return {
        row[0]: (row[1], row[2])
        for row in db.execute(
            "SELECT artifact_id,surfacing,node_retired_at "
            "FROM artifact_registrations ORDER BY artifact_id"
        )
    }


def test_fresh_schema_is_current_version():
    assert SCHEMA_VERSION == 145


def test_migration_restores_recorded_surfacing_only_for_wrongly_retired_workspace_rows(tmp_path):
    path = _legacy_database(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, 22)
        assert (result.migrated, result.from_version, result.to_version) == (True, 21, 22)
        after_first = _registrations(db)
        assert after_first == {
            "durable-visible": ("visible", None),
            "durable-quiet": ("quiet", None),
            "ephemeral": ("settled", "retired"),
            "manually-hidden": ("quiet", "retired"),
            "pinned-non-exempt": ("visible", "retired"),
        }

        assert not migrate_to_current(db, 22).migrated
        assert _registrations(db) == after_first
