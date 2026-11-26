"""The waits an open decision places on plan tasks.

A raised decision waits for the user's answer; a draft or parent-reviewed one
waits for the parent tier's review. Root work has no higher tier, so its author
must act on its own draft and that is no wait on the source task.

The decision's own review task is the person's, but until the raise there is
nothing for them to answer: it waits on the agent tier that reviews and raises
it, so it never reads as an ask (#3401).

``admission_projection`` composes these beside the other waits in
``plans.wait_causes``, so every reader of causes sees them.
"""

from __future__ import annotations

DECISION_RAISED = "decision_raised"
DECISION_REVIEW = "decision_review"
DECISION_NOT_RAISED = "decision_not_raised"


def decision_causes(node_plans, *, db) -> dict[str, list[dict]]:
    """Return the open-decision wait of each node in the batch that has one."""
    from plans import decision_projection

    sources = {str(node["id"]): plan for node, plan in node_plans}
    if not sources:
        return {}
    plan_ids = {str(plan["id"]) for plan in sources.values()}
    rows = [
        row
        for row in decision_projection.rows_for_plans(plan_ids, db=db, open_nodes=True)
        if row["state"] in decision_projection.OPEN_STATES
    ]
    open_rows = [row for row in rows if row["source_node_id"] in sources]
    unraised = [
        row for row in rows
        if row["state"] != decision_projection.RAISED and row["node_id"] in sources
    ]
    if not open_rows and not unraised:
        return {}
    causes: dict[str, list[dict]] = {}
    if unraised:
        tiers = _reviewers(db, {str(sources[row["node_id"]]["owner_id"]) for row in unraised})
        for row in unraised:
            author = str(sources[row["node_id"]]["owner_id"])
            causes[str(row["node_id"])] = [
                not_raised_cause(row, reviewer=tiers.get(author) or author),
            ]
    if not open_rows:
        return causes
    # A raised decision is a wait only while the user can actually answer it.
    # `raised_decisions` is that rule: the body, the attention pile and the
    # answer route must agree, so a question cleared out of band stops holding
    # its task. Read once for the batch.
    answerable = {
        row["id"]
        for row in decision_projection.raised_decisions(plan_ids, db=db, open_nodes=True)
    }
    reviewers = _reviewers(
        db, {str(sources[row["source_node_id"]]["owner_id"]) for row in open_rows},
    )
    for row in open_rows:
        author = str(sources[row["source_node_id"]]["owner_id"])
        cause = _decision_cause(
            row, author=author, reviewer=reviewers.get(author),
            answerable=row["id"] in answerable,
        )
        if cause is not None:
            causes.setdefault(str(row["source_node_id"]), []).append(cause)
    return causes


def not_raised_cause(row: dict, *, reviewer: str) -> dict:
    """The wait on a decision's own review task before its raise (#3401).

    ``reviewer`` is the agent tier that reviews and raises it: the parent that
    delegated the work, or the author itself on root work.
    """
    return {
        "kind": "waiting",
        "reason_key": DECISION_NOT_RAISED,
        "reason": "Not raised yet: the agent reviews it first",
        "owner_kind": "thread",
        "owner_id": reviewer,
        "decision_id": row["id"],
        "created_at": row["created_at"],
    }


def decision_wait_kind(
    state: str, *, author: str, reviewer: str | None, answerable: bool,
) -> str | None:
    """The shared raised/draft parent-tier wait classification."""
    from plans import decision_projection

    if state == decision_projection.RAISED:
        return DECISION_RAISED if answerable else None
    if state not in decision_projection.OPEN_STATES or reviewer is None or reviewer == author:
        return None
    return DECISION_REVIEW


def _decision_cause(
    row: dict, *, author: str, reviewer: str | None, answerable: bool,
) -> dict | None:
    """Return the wait one open decision places on its source task, if any."""
    kind = decision_wait_kind(row["state"], author=author, reviewer=reviewer, answerable=answerable)
    if kind is None:
        return None
    if kind == DECISION_RAISED:
        return {
            "kind": "waiting",
            "reason_key": DECISION_RAISED,
            "reason": "Waiting for the user to answer a decision on this task",
            "owner_kind": "user",
            "owner_id": None,
            "decision_id": row["id"],
            "created_at": row["raised_at"],
        }
    return {
        "kind": "waiting",
        "reason_key": DECISION_REVIEW,
        "reason": "Waiting for the parent to review a decision on this task",
        "owner_kind": "thread",
        "owner_id": reviewer,
        "decision_id": row["id"],
        "created_at": row["created_at"],
    }


def _reviewers(db, authors: set[str]) -> dict[str, str | None]:
    """Return each author thread's parent review tier in one read."""
    if not authors:
        return {}
    marks = ",".join("?" for _ in authors)
    rows = db.execute(
        f"SELECT id, spawned_by FROM threads WHERE id IN ({marks})",
        tuple(sorted(authors)),
    ).fetchall()
    return {str(row["id"]): row["spawned_by"] for row in rows}
