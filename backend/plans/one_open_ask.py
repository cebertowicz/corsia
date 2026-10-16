"""A document has at most one open review request.

The rule's one owner: ``holds.block_hold`` applies it to every user hold on an
artifact, so no other path places a second open request on a document.
"""

from __future__ import annotations

import plans.service as service
from plans.node_states import open_sql, open_user_block_sql


def already_open(db, node_id: str, artifact_id: str, *, reason_key: str, reason: str) -> bool:
    """Apply the rule before a user hold on ``artifact_id`` is placed.

    True when this task already holds that request with the same reason, so the
    caller writes nothing. Any other open request on the document, from another
    task or this one with a new reason, is refused, naming the hold to resolve
    before asking again. A retired task's hold is not counted, so a task that
    ended without withdrawing never locks its document. One indexed read by
    artifact (``idx_plan_node_artifact_links_artifact``), on a user block only.
    """
    row = db.execute(
        f"""SELECT h.id AS hold_id, h.reason_key, h.reason, n.id AS node_id, n.title
              FROM plan_node_artifact_links l
              JOIN plan_nodes n ON n.id = l.node_id
              JOIN plan_node_holds h ON h.node_id = n.id
             WHERE l.artifact_id=? AND l.role='attention' AND {open_sql('n')}
               AND {open_user_block_sql('h')} AND h.owner_id=l.artifact_id
             ORDER BY h.node_id=? DESC, h.created_at DESC, h.id DESC
             LIMIT 1""",
        (artifact_id, node_id),
    ).fetchone()
    if row is None:
        return False
    if row["node_id"] == node_id and (row["reason_key"], row["reason"]) == (reason_key, reason):
        return True
    raise service.PlanInputError(
        f"this document already has an open review request on task {row['title']!r} "
        f"(node_id {row['node_id']}, hold_id {row['hold_id']}); "
        "resolve that blocker, then ask again"
    )
