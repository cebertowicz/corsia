"""Ordered project account policy and durable per-vendor execution holds."""
from __future__ import annotations

from infra.vendor_account_schema import VENDOR_ACCOUNT_SCHEMA_SQL

VENDOR_POLICY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS provider_policies (
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    provider_key TEXT NOT NULL CHECK (length(trim(provider_key)) > 0),
    rollover INTEGER NOT NULL DEFAULT 0 CHECK (rollover IN (0,1)),
    order_json TEXT CHECK (order_json IS NULL OR (json_valid(order_json) AND json_type(order_json)='array')),
    disabled_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(disabled_json) AND json_type(disabled_json)='array'),
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_policies_scope
    ON provider_policies(ifnull(project_id,''), provider_key);
CREATE TABLE IF NOT EXISTS project_policy_overrides (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS thread_vendor_account_holds (
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('claude-code','codex')),
    account_id TEXT REFERENCES vendor_accounts(id),
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(thread_id,provider)
);
"""

VENDOR_SUBSCRIPTION_SCHEMA_SQL = VENDOR_ACCOUNT_SCHEMA_SQL + VENDOR_POLICY_SCHEMA_SQL
