"""Schema 119 -> 120: withdraw attention stranded by cleared review asks."""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_TABLES = (
    "artifact_identities", "plan_node_artifact_links", "plan_node_events", "plan_node_holds", "plan_nodes",
)


def _stale_review_attention_sql() -> str:
    """Raised attention an ended user ask left behind, and nothing else.

    It takes positive evidence: a user ask on the item ended and none is open.
    Attention a parent review approved or deferred (``disposition_outcome``) or
    a raised plan decision holds stays raised: neither has an ask behind it.
    """
    from plans.public import open_user_block_sql  # lazy: plans imports infra

    return f"""
    SELECT i.id
      FROM artifact_identities i
     WHERE i.state='active'
       AND json_extract(i.metadata, '$.attention_state')='raised'
       AND json_extract(i.metadata, '$.disposition_outcome') IS NULL
       AND COALESCE(json_extract(i.metadata, '$.plan_decision.state'), '') != 'raised'
       AND EXISTS (
           SELECT 1
             FROM plan_node_artifact_links l
             JOIN plan_node_holds h ON h.node_id=l.node_id
            WHERE l.artifact_id=i.id AND l.role='attention'
              AND h.kind='blocked' AND h.owner_kind='user'
              AND h.owner_id=l.artifact_id AND h.cleared_at IS NOT NULL
       )
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
    """Repair retired asks, then withdraw their no-longer-raised attention."""
    cleared = withdraw_retired_user_asks(conn)
    count = withdraw_stale_review_attention(conn)
    log.info("Withdrew %d retired review ask(s) and %d stale attention item(s)", cleared, count)


def withdraw_retired_user_asks(conn: sqlite3.Connection) -> int:
    """Clear orphaned user asks only when their task is positively retired."""
    from plans.public import TERMINAL_STATES, clear_hold_write

    present = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not present.issuperset(_TABLES):
        return 0
    terminal = tuple(sorted(TERMINAL_STATES))
    marks = ",".join("?" for _ in terminal)
    rows = conn.execute(
        f"""SELECT h.id AS hold_id,h.node_id
              FROM plan_node_holds h
              JOIN plan_node_artifact_links l
                ON l.node_id=h.node_id AND l.artifact_id=h.owner_id AND l.role='attention'
              JOIN plan_nodes n ON n.id=h.node_id
             WHERE h.kind='blocked' AND h.owner_kind='user' AND h.owner_id IS NOT NULL
               AND h.cleared_at IS NULL
               AND (n.archived_at IS NOT NULL OR n.state IN ({marks}))
             ORDER BY h.created_at,h.id""",
        terminal,
    ).fetchall()
    for row in rows:
        # clear_hold_write only appends the hold event and touches the node: it neither
        # validates live state nor reconciles admission, so it is migration-safe.
        clear_hold_write(
            str(row["node_id"]), str(row["hold_id"]), expected_kind="blocked",
            actor_id=None, provenance="migration.119_to_120", detail={"ended": "withdrawn"}, db=conn,
        )
    return len(rows)


def withdraw_stale_review_attention(conn: sqlite3.Connection) -> int:
    """Use attention's canonical writer for stale raised review asks, once each."""
    from registry import attention

    present = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not present.issuperset(_TABLES):
        return 0  # an older partial schema holds no review asks to repair
    artifact_ids = [row[0] for row in conn.execute(_stale_review_attention_sql())]
    for artifact_id in artifact_ids:
        attention.withdraw_attention(str(artifact_id), db=conn)
    return len(artifact_ids)
