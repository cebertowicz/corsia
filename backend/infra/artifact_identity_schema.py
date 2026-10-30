"""Canonical artifact-identity table and index schema."""

from __future__ import annotations


#: A decision's source task finds its decisions by this key, so its waiting time
#: costs its own decisions, never every decision ever raised (``plans.node_waiting``).
#: Partial on well-formed decision rows: an expression index evaluates every row it covers.
DECISION_SOURCE_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_artifact_identities_decision_source
    ON artifact_identities(json_extract(metadata, '$.plan_decision.source_node_id'))
    WHERE integration='local' AND type='plan-decision' AND json_valid(metadata);
"""

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
""" + DECISION_SOURCE_INDEX_SQL
