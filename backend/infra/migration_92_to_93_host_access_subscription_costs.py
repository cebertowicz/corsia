"""Clear list-price estimates from historical canonical subscription rows."""
from __future__ import annotations


def migrate_92_to_93(conn) -> None:
    """Keep tokens while clearing non-charge dollars from subscription harness rows."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "llm_usage" not in tables:
        return
    conn.execute(
        """UPDATE llm_usage
           SET cost_usd = NULL
           WHERE cost_usd IS NOT NULL
             AND (model GLOB 'claude-code/*' OR model GLOB 'codex/*')"""
    )
