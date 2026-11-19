"""Schema 200 -> 201: tables for each plan node's usage grouped at write.

Purely additive; runs inside the migration runner's transaction and never commits.
It creates the empty tables only. The triggers and the backfill are not built here:
``core.node_usage_groups.ensure`` runs right after the migrations, before the server
serves, and fills the rows in short transactions over node ranges so no single write
holds the lock for the whole ledger. Until it has, every reader reads the ledger.
"""

from __future__ import annotations

import sqlite3

from infra.node_usage_schema import NODE_USAGE_TABLES


def migrate_200_to_201(conn: sqlite3.Connection) -> None:
    """Create the fresh-schema tables when the usage table exists."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_usage'").fetchone():
        for statement in NODE_USAGE_TABLES.values():
            conn.execute(statement)
