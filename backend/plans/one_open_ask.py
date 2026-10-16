"""A document has at most one open review request.

The rule's one owner: ``holds.block_hold`` applies it to every user hold on an
artifact, so no other path places a second open request on a document. What
counts as open is ``user_ask.open_asks_sql``, the definition the review bar and
the answer read, so the rule holds in what the user sees.
"""

from __future__ import annotations

import plans.service as service
from plans.user_ask import open_asks_sql


def already_open(
    db, node_id: str, artifact_id: str, *, reason_key: str, reason: str,
    actor_id: str | None, provenance: str,
) -> bool:
    """Apply the rule before a user hold on ``artifact_id`` is placed.

    True when this task already holds that request with the same reason, so the
    caller writes nothing. This task's own request with a new reason is ended as
    replaced in the caller's transaction, and False lets the caller place the
    new one: the user reads one current request. Another task's open request is
    refused, naming its task and hold: it is the user's to answer. Every way a
    task ends withdraws its requests, so that task is live. One indexed read by
    artifact (``idx_plan_node_artifact_links_artifact``), on a user block only.
    """
    row = db.execute(
        open_asks_sql("h.id AS hold_id, h.reason_key, h.reason, n.id AS node_id, n.title")
        + " ORDER BY h.node_id=? DESC, h.created_at DESC, h.id DESC LIMIT 1",
        (artifact_id, node_id),
    ).fetchone()
    if row is None:
        return False
    held = f"task {row['title']!r} (node_id {row['node_id']}, hold_id {row['hold_id']})"
    if row["node_id"] != node_id:
        raise service.PlanInputError(
            f"this document already has an open review request on {held}, waiting for "
            "the user's answer. It is theirs to answer, so do not resolve it; ask again "
            "once they have answered it"
        )
    if (row["reason_key"], row["reason"]) == (reason_key, reason):
        return True
    from plans import ask_endings, holds

    holds.clear_write(
        node_id, str(row["hold_id"]), expected_kind="blocked", actor_id=actor_id,
        provenance=provenance, detail=ask_endings.detail(why="replaced", actor_id=actor_id), db=db,
    )
    return False
