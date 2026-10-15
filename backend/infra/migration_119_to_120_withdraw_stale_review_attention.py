"""Schema 119 -> 120: withdraw attention stranded by cleared review asks."""
from __future__ import annotations

import logging
import sqlite3

from plans.node_states import open_user_block_sql

log = logging.getLogger(__name__)

_TABLES = ("artifact_identities", "plan_node_artifact_links", "plan_node_holds")


_STALE_REVIEW_ATTENTION_SQL = f"""
    SELECT i.id
      FROM artifact_identities i
     WHERE i.state='active'
       AND json_extract(i.metadata, '$.attention_state')='raised'
       AND COALESCE(json_extract(i.metadata, '$.plan_decision.state'), '') != 'raised'
       AND NOT EXISTS (
           SELECT 1
             FROM plan_node_artifact_links l
             JOIN plan_node_holds h ON h.node_id=l.node_id
            WHERE l.artifact_id=i.id AND l.role='attention'
              AND {open_user_block_sql('h')} AND h.owner_id=l.artifact_id
       )
     ORDER BY i.id
"""


def migrate_119_to_120(conn: sqlite3.Connection) -> None:
    """Withdraw raised review attention whose live cause was cleared before this repair."""
    count = withdraw_stale_review_attention(conn)
    log.info("Withdrew %d stale review attention item(s)", count)


def withdraw_stale_review_attention(conn: sqlite3.Connection) -> int:
    """Use attention's canonical writer for stale raised review asks, once each."""
    from registry import attention

    present = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not present.issuperset(_TABLES):
        return 0  # an older partial schema holds no review asks to repair
    artifact_ids = [row[0] for row in conn.execute(_STALE_REVIEW_ATTENTION_SQL)]
    for artifact_id in artifact_ids:
        attention.withdraw_attention(str(artifact_id), db=conn)
    return len(artifact_ids)
