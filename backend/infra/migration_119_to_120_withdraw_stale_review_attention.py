"""Schema 119 -> 120: withdraw attention stranded by cleared review asks."""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_TABLES = (
    "artifact_identities", "plan_node_artifact_links", "plan_node_events", "plan_node_holds", "plan_nodes",
)


def _stale_review_attention_sql() -> str:
    """Raised or deferred attention an ended user ask may have left behind: the candidates.

    It takes positive evidence: a user ask on the item ended. Whether anything
    still raises it (another open ask, a parent review's approval or deferral, a
    raised plan decision) is not restated here: each candidate goes to
    ``sync_ask_attention``, the reconciler every runtime ask edge calls, so the
    repair and the runtime agree on which attention an ask owns. Attention no
    ask ever touched is never a candidate. Deferred attention is a candidate
    too: a deferral the user chose on an ask whose task has since ended would
    otherwise come back when its condition fires, for a request already gone.
    """
    return """
    SELECT i.id
      FROM artifact_identities i
     WHERE i.state='active'
       AND json_extract(i.metadata, '$.attention_state') IN ('raised','deferred')
       AND EXISTS (
           SELECT 1
             FROM plan_node_artifact_links l
             JOIN plan_node_holds h ON h.node_id=l.node_id
            WHERE l.artifact_id=i.id AND l.role='attention'
              AND h.kind='blocked' AND h.owner_kind='user'
              AND h.owner_id=l.artifact_id AND h.cleared_at IS NOT NULL
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
    """Reconcile each candidate through the runtime's ask reconciler, once each.

    ``sync_ask_attention`` withdraws a candidate only when nothing still raises
    it, through attention's canonical writer. Returns how many it withdrew.
    """
    from plans.public import sync_ask_attention  # lazy: plans imports infra

    present = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if not present.issuperset(_TABLES):
        return 0  # an older partial schema holds no review asks to repair
    artifact_ids = [row[0] for row in conn.execute(_stale_review_attention_sql())]
    return sum(
        sync_ask_attention(conn, str(artifact_id), outcome="withdraw") is not None
        for artifact_id in artifact_ids
    )
