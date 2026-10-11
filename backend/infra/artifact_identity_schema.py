"""Canonical artifact-identity table and index schema."""

from __future__ import annotations


ARTIFACT_IDENTITY_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS artifact_identities (
        id TEXT PRIMARY KEY,
        integration TEXT NOT NULL,
        type TEXT NOT NULL,
        external_id TEXT NOT NULL,
        title TEXT,
        description TEXT,
        path TEXT,
        state TEXT NOT NULL DEFAULT 'active',
        retention TEXT NOT NULL DEFAULT 'durable'
            CHECK (retention IN ('ephemeral', 'durable')),
        metadata TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(integration, type, external_id)
    );
CREATE INDEX IF NOT EXISTS idx_artifact_identities_upload_path
    ON artifact_identities(path) WHERE integration='upload';
"""
