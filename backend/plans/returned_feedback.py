"""The feedback a revision task was created with, until evidence answers it.

One owner for one fact: whether a task is a revision whose feedback no new
evidence has answered, and what that feedback said. The next-task block that
hands the executor its revision reads it here, and so does anything else
that needs it, so no two readers can disagree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StandingFeedback:
    """Feedback still unanswered: its text, or None when unreadable."""

    feedback: str | None

    @property
    def readable(self) -> bool:
        """Whether the recorded feedback could be read."""
        return self.feedback is not None


def standing_feedback(db, node_id: str) -> StandingFeedback | None:
    """Return the task's unanswered revision feedback, or None when there is none.

    None when the task is no revision, or its latest event of the two is
    submitted evidence. A revision whose recorded payload cannot be read, or
    names no feedback, is returned unreadable rather than as absent feedback:
    the corrupt consumed contract surfaces wherever it is read, so a handoff
    says the reason is missing instead of dropping it.
    """
    row = db.execute(
        """SELECT event,payload_json FROM plan_node_events
           WHERE node_id=? AND event IN ('revision_created','review_evidence_submitted')
           ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (node_id,),
    ).fetchone()
    if row is None or row["event"] != "revision_created":
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return StandingFeedback(None)
    feedback = payload.get("feedback") if isinstance(payload, dict) else None
    if not isinstance(feedback, str) or not feedback:
        return StandingFeedback(None)
    return StandingFeedback(feedback)
