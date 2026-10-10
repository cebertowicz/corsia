"""Frozen schema80→81 subscription identities and immutable spend references.

Move the R1 usage object unchanged into one Default per existing harness
connection. Runtime has no legacy read after this transaction. Historical
Turns and spend remain unknown; no account is inferred retroactively.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

# Frozen DDL; never import runtime schema, account, or usage code here.
_ACCOUNT_SQL = """
CREATE TABLE IF NOT EXISTS vendor_accounts (
    id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0), source_home TEXT, runtime_home TEXT,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
    state TEXT NOT NULL DEFAULT 'signed_out' CHECK (state IN ('legacy','signed_in','signed_out','removed')),
    revision INTEGER NOT NULL DEFAULT 0,
    identity_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(identity_json)),
    auth_cleanup_json TEXT CHECK (auth_cleanup_json IS NULL OR
        (json_valid(auth_cleanup_json) AND json_type(auth_cleanup_json)='object')),
    discovery_json TEXT CHECK (discovery_json IS NULL OR json_valid(discovery_json)),
    usage_json TEXT CHECK (usage_json IS NULL OR json_valid(usage_json)),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, removed_at TEXT,
    CHECK ((source_home IS NULL AND runtime_home IS NULL) OR
           (source_home IS NOT NULL AND runtime_home IS NOT NULL AND source_home != runtime_home))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_accounts_default
    ON vendor_accounts(connection_id) WHERE is_default=1;
CREATE INDEX IF NOT EXISTS idx_vendor_accounts_connection ON vendor_accounts(connection_id,created_at);
CREATE TABLE IF NOT EXISTS thread_vendor_accounts (
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('claude-code','codex')),
    account_id TEXT NOT NULL REFERENCES vendor_accounts(id), PRIMARY KEY(thread_id,provider)
);
"""


def _add_account_column(conn: sqlite3.Connection, table: str) -> None:
    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if columns and "vendor_account_id" not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN vendor_account_id TEXT REFERENCES vendor_accounts(id)")


def _adopt_connections(conn: sqlite3.Connection) -> None:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "connections" not in tables:
        return  # Historical partial-schema rehearsals have no transport to adopt.
    rows = conn.execute("SELECT id,provider,config,created_at,updated_at FROM connections "
                        "WHERE provider IN ('claude-code','codex')").fetchall()
    for connection_id, provider, raw, created, updated in rows:
        config = json.loads(raw) if raw else {}
        if not isinstance(config, dict):
            config = {}
        had_usage = "usage" in config
        usage = config.pop("usage", None)
        existing = conn.execute("SELECT id FROM vendor_accounts WHERE connection_id=? AND is_default=1",
                                (connection_id,)).fetchone()
        if existing is None:
            account_id = uuid.uuid4().hex
            # Literal on purpose; connections.vendor_accounts.DEFAULT_ACCOUNT_NAME is the live constant.
            conn.execute("""INSERT INTO vendor_accounts
                (id,connection_id,name,is_default,state,usage_json,created_at,updated_at)
                VALUES (?,?,'Default',1,'legacy',?,?,?)""",
                (account_id, connection_id, json.dumps(usage) if isinstance(usage, dict) else None, created, updated))
        else:
            account_id = str(existing[0])
        if had_usage:
            conn.execute("UPDATE connections SET config=? WHERE id=?", (json.dumps(config), connection_id))
        if not {"threads", "models"}.issubset(tables):
            continue
        # Frozen catalog lookup: exact id wins; a transport alias must be unique.
        conn.execute("""INSERT OR IGNORE INTO thread_vendor_accounts(thread_id,provider,account_id)
            SELECT t.id,?,? FROM threads t WHERE
                EXISTS (SELECT 1 FROM models m WHERE m.id=t.model AND m.harness=?)
                OR (NOT EXISTS (SELECT 1 FROM models m WHERE m.id=t.model)
                    AND (SELECT COUNT(*) FROM models m WHERE m.litellm_model=t.model)=1
                    AND EXISTS (SELECT 1 FROM models m WHERE m.litellm_model=t.model AND m.harness=?))""",
            (provider, account_id, provider, provider))


def _separate_cleanup(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(vendor_accounts)")}
    if "auth_cleanup_json" not in columns:
        conn.execute("ALTER TABLE vendor_accounts ADD COLUMN auth_cleanup_json TEXT CHECK "
                     "(auth_cleanup_json IS NULL OR (json_valid(auth_cleanup_json) "
                     "AND json_type(auth_cleanup_json)='object'))")
    for account_id, raw_identity, raw_cleanup in conn.execute(
        "SELECT id,identity_json,auth_cleanup_json FROM vendor_accounts"
    ).fetchall():
        identity = json.loads(raw_identity)
        cleanup = json.loads(raw_cleanup) if raw_cleanup is not None else None
        if raw_cleanup is not None and not isinstance(cleanup, dict):
            raise ValueError("account_auth_cleanup_invalid")
        if not isinstance(identity, dict) or "auth_cleanup" not in identity:
            continue
        old = identity.pop("auth_cleanup")
        if old is not None and not isinstance(old, dict):
            raise ValueError("account_auth_cleanup_invalid")
        if cleanup is not None and old is not None and cleanup != old:
            raise ValueError("account_auth_cleanup_conflict")
        canonical = raw_cleanup if raw_cleanup is not None else json.dumps(old) if old is not None else None
        conn.execute("UPDATE vendor_accounts SET identity_json=?,auth_cleanup_json=? WHERE id=?",
                     (json.dumps(identity), canonical, account_id))


def migrate_80_to_81(conn: sqlite3.Connection) -> None:
    """Runs within the migration runner's transaction; never commits."""
    for statement in _ACCOUNT_SQL.split(";"):
        if statement.strip():
            conn.execute(statement)
    _separate_cleanup(conn)
    _add_account_column(conn, "active_turns")
    _add_account_column(conn, "llm_usage")
    _adopt_connections(conn)
