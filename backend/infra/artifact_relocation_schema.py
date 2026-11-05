"""The durable recovery journal for ``artifact_relocate``, kept beside the other domain schema modules."""

from __future__ import annotations

ARTIFACT_RELOCATION_SCHEMA_SQL = """
    -- Durable recovery journal for artifact_relocate: filesystem rename and registry
    -- identity update are not one transaction, so an interrupted relocation is
    -- deterministically completed or cleaned up on discovery. target_type/target_title
    -- carry the FULL target identity so recovery reaches the same end state as the
    -- uninterrupted path; they are nullable only so a pre-existing in-flight intent can
    -- still be recovered (the writer always populates them).
    CREATE TABLE IF NOT EXISTS artifact_relocation_intents (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
        actor_thread_id TEXT NOT NULL,
        source_path TEXT NOT NULL,
        target_path TEXT NOT NULL,
        source_external_id TEXT NOT NULL,
        target_external_id TEXT NOT NULL,
        target_type TEXT,
        target_title TEXT,
        phase TEXT NOT NULL CHECK (phase IN ('recorded', 'file_moved', 'identity_updated')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
"""
