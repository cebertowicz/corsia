"""The harvest's review event: the one SQL reading both of its readers share.

``delegation_harvest.harvest_review`` asks it for one node and
``delegation_owed_verdict.owed_verdicts`` for every node in one query, so the
two cannot disagree about which event is a step's latest review.
"""

from __future__ import annotations

#: The (event, provenance) of the harvest's own review transition.
HARVEST_EVENT = ("state_transition", "plan.delegation_harvest")


def latest_review_event_sql(columns: str, node_ref: str) -> str:
    """The newest review event of the node ``node_ref``, as a one-row SELECT.

    The one reading of "the step's latest review event", so the per-node and
    the batched reads of ``harvest_review`` cannot disagree. ``node_ref`` is a
    placeholder or a column of an enclosing query.
    """
    return (
        f"SELECT {columns} FROM plan_node_events AS review_event"
        f" WHERE review_event.node_id={node_ref}"
        f" AND ((review_event.event='state_transition'"
        f" AND review_event.new_state='review_requested')"
        f" OR review_event.event='review_evidence_submitted')"
        f" ORDER BY review_event.created_at DESC,review_event.rowid DESC LIMIT 1"
    )
