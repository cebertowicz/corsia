
"""Startup migration 8 -> 9 proof for legacy plan-control residue."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from test_schema_migration_v8 import _seed_v7, _v7_db


def _schema_8_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE artifact_identities (
            id TEXT PRIMARY KEY,
            integration TEXT NOT NULL,
            type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT,
            description TEXT,
            path TEXT,
            state TEXT NOT NULL DEFAULT 'active',
            retention TEXT NOT NULL DEFAULT 'durable',
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(integration, type, external_id)
        );
        CREATE TABLE artifact_registrations (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            parent_thread_id TEXT NOT NULL,
            relationship_state TEXT NOT NULL DEFAULT 'owner-active',
            tmp INTEGER NOT NULL DEFAULT 0,
            surfacing TEXT NOT NULL DEFAULT 'visible',
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(artifact_id, parent_thread_id)
        );
        CREATE TABLE plan_node_artifact_links (
            node_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            role TEXT NOT NULL,
            actor_id TEXT,
            provenance TEXT NOT NULL,
            source_event_id TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (node_id, artifact_id, role)
        ) WITHOUT ROWID;
        PRAGMA user_version=8;
        """
    )
    return db


def _identity(
    db: sqlite3.Connection,
    artifact_id: str,
    artifact_type: str,
    *,
    external_id: str | None = None,
    state: str = "active",
    retention: str = "durable",
    plan_node_id: str | None = None,
) -> None:
    metadata = {"marker": artifact_id}
    if plan_node_id is not None:
        metadata["plan_node_id"] = plan_node_id
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,title,state,retention,metadata,
            created_at,updated_at)
           VALUES (?,'local',?,?,?,?,?,?,'created','before')""",
        (
            artifact_id,
            artifact_type,
            external_id or f"external-{artifact_id}",
            artifact_id,
            state,
            retention,
            json.dumps(metadata, sort_keys=True),
        ),
    )


def _link(
    db: sqlite3.Connection,
    artifact_id: str,
    role: str,
    *,
    node_id: str,
) -> None:
    db.execute(
        """INSERT INTO plan_node_artifact_links
           (node_id,artifact_id,role,provenance,created_at)
           VALUES (?,?,?,'test','created')""",
        (node_id, artifact_id, role),
    )


def _registration(
    db: sqlite3.Connection,
    artifact_id: str,
    thread_id: str,
    relationship_state: str,
) -> None:
    db.execute(
        """INSERT INTO artifact_registrations
           (id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at)
           VALUES (?,?,?,?,'created','before')""",
        (f"reg-{artifact_id}-{thread_id}", artifact_id, thread_id, relationship_state),
    )


def test_migration_tombstones_only_legacy_generated_plan_controls(tmp_path: Path) -> None:
    from infra.database_migrations import migrate_to_current

    path = tmp_path / "corsia.db"
    db = _schema_8_db(path)
    try:
        _identity(
            db,
            "archived-control",
            "plan-node",
            external_id="node-archived",
            state="archived",
            plan_node_id="node-archived",
        )
        _identity(
            db,
            "active-control",
            "plan-node",
            external_id="node-active",
            plan_node_id="node-active",
        )
        _identity(db, "user-plan-link", "plan-node", external_id="node-user")
        _identity(
            db,
            "ephemeral-control",
            "plan-node",
            external_id="node-ephemeral",
            retention="ephemeral",
            plan_node_id="node-ephemeral",
        )
        _identity(db, "result-link", "plan-node", external_id="node-result")
        _identity(
            db,
            "mismatched-link-control",
            "plan-node",
            external_id="node-mismatched-link",
            plan_node_id="node-mismatched-link",
        )
        _identity(
            db,
            "mismatched-metadata-control",
            "plan-node",
            external_id="node-mismatched-metadata",
            plan_node_id="other-node",
        )
        _identity(db, "decision", "plan-decision", retention="durable")
        _identity(db, "document", "document", retention="durable")
        _link(
            db, "archived-control", "attention", node_id="node-archived"
        )
        _link(db, "active-control", "attention", node_id="node-active")
        _link(db, "user-plan-link", "attention", node_id="node-user")
        _link(db, "ephemeral-control", "attention", node_id="node-ephemeral")
        _link(db, "mismatched-link-control", "attention", node_id="other-node")
        _link(
            db,
            "mismatched-metadata-control",
            "attention",
            node_id="node-mismatched-metadata",
        )
        _link(db, "result-link", "result", node_id="node-result")
        _link(db, "decision", "attention", node_id="decision-node")
        _link(db, "document", "attention", node_id="document-node")
        _registration(db, "archived-control", "owner", "owner-active")
        _registration(db, "archived-control", "subscriber", "subscriber-recent")
        _registration(db, "document", "document-owner", "owner-active")
        db.execute(
            "UPDATE artifact_registrations SET surfacing='quiet' WHERE artifact_id='document'"
        )
        db.commit()

        untouched_ids = (
            "user-plan-link",
            "result-link",
            "mismatched-link-control",
            "mismatched-metadata-control",
            "decision",
            "document",
        )
        before = {
            row["id"]: tuple(row)
            for row in db.execute("SELECT * FROM artifact_identities ORDER BY id")
            if row["id"] in untouched_ids
        }
        document_registration = tuple(
            db.execute(
                "SELECT * FROM artifact_registrations WHERE artifact_id='document'"
            ).fetchone()
        )
        result = migrate_to_current(db, 9)

        assert result.migrated and (result.from_version, result.to_version) == (8, 9)
        states = {
            row["id"]: row["state"]
            for row in db.execute("SELECT id,state FROM artifact_identities")
        }
        assert states["archived-control"] == "tombstone"
        assert states["active-control"] == "tombstone"
        assert states["ephemeral-control"] == "tombstone"
        assert {
            row["id"]: tuple(row)
            for row in db.execute("SELECT * FROM artifact_identities ORDER BY id")
            if row["id"] in untouched_ids
        } == before
        assert tuple(
            db.execute(
                "SELECT * FROM artifact_registrations WHERE artifact_id='document'"
            ).fetchone()
        ) == document_registration
        registrations = {
            row["parent_thread_id"]: row["relationship_state"]
            for row in db.execute(
                """SELECT parent_thread_id,relationship_state
                   FROM artifact_registrations
                   WHERE artifact_id='archived-control'"""
            )
        }
        assert registrations == {"owner": "owner-active", "subscriber": "tombstoned"}
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
    finally:
        db.close()


def test_migration_preserves_invalid_and_non_object_metadata(tmp_path: Path) -> None:
    from infra.database_migrations import migrate_to_current

    db = _schema_8_db(tmp_path / "corsia.db")
    metadata_cases = {
        "null-metadata": None,
        "empty-metadata": "",
        "malformed-metadata": "not json",
        "array-metadata": json.dumps(["plan_node_id"]),
        "string-metadata": json.dumps("plan_node_id"),
    }
    try:
        for artifact_id, metadata in metadata_cases.items():
            node_id = f"node-{artifact_id}"
            _identity(db, artifact_id, "plan-node", external_id=node_id)
            _link(db, artifact_id, "attention", node_id=node_id)
            db.execute(
                "UPDATE artifact_identities SET metadata=? WHERE id=?",
                (metadata, artifact_id),
            )
        db.commit()
        before = [
            tuple(row)
            for row in db.execute("SELECT * FROM artifact_identities ORDER BY id")
        ]

        result = migrate_to_current(db, 9)

        assert result.migrated
        assert [
            tuple(row)
            for row in db.execute("SELECT * FROM artifact_identities ORDER BY id")
        ] == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == 9
    finally:
        db.close()


def test_startup_ladder_retires_only_faithful_v7_control(tmp_path: Path) -> None:
    from infra.database_migrations import migrate_to_current

    db = _v7_db(tmp_path / "corsia.db")
    try:
        db.execute(
            "ALTER TABLE artifact_identities ADD COLUMN retention "
            "TEXT NOT NULL DEFAULT 'durable'"
        )
        db.executescript(
            """
            CREATE TABLE artifact_registrations (
                id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
                parent_thread_id TEXT NOT NULL,
                relationship_state TEXT NOT NULL DEFAULT 'owner-active',
                tmp INTEGER NOT NULL DEFAULT 0,
                surfacing TEXT NOT NULL DEFAULT 'visible',
                metadata TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(artifact_id,parent_thread_id)
            );
            """
        )
        _seed_v7(db)
        db.execute(
            """INSERT INTO artifact_identities
               (id,integration,type,external_id,state,retention,metadata,
                created_at,updated_at)
               VALUES ('faithful-control','local','plan-node','child-node',
                       'active','durable',?,'t','t')""",
            (json.dumps({"attention_state": "raised", "plan_node_id": "child-node"}),),
        )
        db.execute(
            """INSERT INTO plan_node_artifact_links
               (node_id,artifact_id,role,provenance,created_at)
               VALUES ('child-node','faithful-control','attention','test','t')"""
        )
        for artifact_id in ("faithful-control", "generic-blocker"):
            db.execute(
                """INSERT INTO artifact_registrations
                   (id,artifact_id,parent_thread_id,relationship_state,
                    created_at,updated_at)
                   VALUES (?,?,?,'subscriber-recent','t','before')""",
                (f"reg-{artifact_id}", artifact_id, f"thread-{artifact_id}"),
            )
        db.commit()

        result = migrate_to_current(db, 9)

        assert result.migrated and (result.from_version, result.to_version) == (7, 9)
        states = dict(
            db.execute(
                """SELECT id,state FROM artifact_identities
                   WHERE id IN ('faithful-control','generic-blocker','real-decision')"""
            )
        )
        assert states == {
            "faithful-control": "tombstone",
            "generic-blocker": "archived",
            "real-decision": "active",
        }
        assert dict(
            db.execute(
                "SELECT artifact_id,relationship_state FROM artifact_registrations"
            )
        ) == {
            "faithful-control": "tombstoned",
            "generic-blocker": "subscriber-recent",
        }
    finally:
        db.close()


def test_migration_is_idempotent_at_schema_9(tmp_path: Path) -> None:
    from infra.database_migrations import migrate_to_current
    from infra.migration_8_to_9_artifacts import migrate_8_to_9

    db = _schema_8_db(tmp_path / "corsia.db")
    try:
        _identity(
            db,
            "archived-control",
            "plan-node",
            external_id="node-archived",
            state="archived",
            plan_node_id="node-archived",
        )
        _link(db, "archived-control", "attention", node_id="node-archived")
        _registration(db, "archived-control", "subscriber", "subscriber-recent")
        db.commit()

        first = migrate_to_current(db, 9)
        after_first = (
            tuple(db.execute("SELECT * FROM artifact_identities").fetchone()),
            tuple(db.execute("SELECT * FROM artifact_registrations").fetchone()),
        )
        migrate_8_to_9(db)
        second = migrate_to_current(db, 9)

        assert first.migrated
        assert not second.migrated
        assert (
            tuple(db.execute("SELECT * FROM artifact_identities").fetchone()),
            tuple(db.execute("SELECT * FROM artifact_registrations").fetchone()),
        ) == after_first
    finally:
        db.close()
