"""The user's verdict on a document or image an agent asked them to review (DCOL-FR12.4).

One round is one user hold: it runs from the ask's raise to the user's answer.
The round's feedback is what the user authored on the artifact after the raise,
which is Documents state: its comments, suggestions and edits are counted here,
and answering closes the user's open feedback batch through the same writer as
send-on-close, so the comments reach the owner once as the usual feedback task.
Plans owns the hold; this module composes the answer and hands Plans the
release it applies (``plans.public.AskRelease``): an opaque payload for the
clearance event and Plans-owned agent wake.
``shared/documents/DRAFT-review-bar-design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from doceditor import feedback_batches
from doceditor.feedback_state import USER_AUTHOR_ID

VERDICTS = {
    "approved": "approved",
    "approved_with_changes": "approved with changes",
    "changes_requested": "requested changes on",
}
NOTE_MAX = 2000
NOTE_REQUIRED = "Say what should change: this round has no comments, suggestions or edits."
#: An answer whose request is no longer the open one: it ended, or a newer
#: same-task ask replaced the one the person read (``answer``'s ``hold_ids``).
STALE = "This review request has ended."


class AnswerRefused(ValueError):
    """The verdict cannot be recorded; ``code`` is the typed reason a client branches on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ReviewAnswer:
    """What the user decided, as the clearance event carries it."""

    verdict: str
    note: str | None = None
    counts: dict = field(default_factory=dict)
    feedback_task: str | None = None

    def payload(self) -> dict:
        return {
            "verdict": self.verdict, "note": self.note,
            "feedback_counts": self.counts,
        }

    def release(self):
        """Return the answer payload Plans records and delivers."""
        from plans.public import AskRelease

        return AskRelease(detail=self.payload())


def round_counts(db, artifact_id: str, since: str | None) -> dict:
    """The comments, suggestions and edits the user made on the artifact since ``since``.

    ``since`` is the ask's raise stamp. Plans and Documents both write UTC ISO-8601
    (``plans.service._now``, ``doceditor.comments._now``), so the string comparison is exact.
    """
    floor = since or ""
    rows = db.execute(
        """SELECT SUM(replacement IS NULL) AS comments,
                  SUM(replacement IS NOT NULL) AS suggestions
             FROM doc_comments
            WHERE artifact_id=? AND author_kind='user' AND created_at>?""",
        (artifact_id, floor),
    ).fetchone()
    edits = db.execute(
        """SELECT COUNT(*) FROM doc_feedback_edit_items e
             JOIN doc_feedback_batches b ON b.id=e.batch_id
            WHERE b.state='open' AND b.author_kind='user' AND b.author_id=?
              AND b.artifact_id=? AND e.last_at>?
              AND (e.prior_segments_diff<>'' OR e.latest_sha256<>e.baseline_sha256)""",
        (USER_AUTHOR_ID, artifact_id, floor),
    ).fetchone()[0]
    # An edit item is deleted once its batch is delivered, but the batch keeps its item count, which
    # is its comment rows plus the one edit item: the difference is the edits the user already sent.
    sent_edits = db.execute(
        """SELECT COALESCE(SUM(MAX(b.item_count - (
                    SELECT COUNT(*) FROM doc_feedback_batch_items i WHERE i.batch_id=b.id), 0)), 0)
             FROM doc_feedback_batches b
            WHERE b.state='delivered' AND b.author_kind='user' AND b.author_id=?
              AND b.artifact_id=? AND b.last_item_at>?""",
        (USER_AUTHOR_ID, artifact_id, floor),
    ).fetchone()[0]
    return {
        "comments": rows["comments"] or 0, "suggestions": rows["suggestions"] or 0,
        "edits": edits + sent_edits,
    }


def _feedback_task(db, artifact_id: str, since: str | None) -> str | None:
    """The title of the newest feedback task the user's batches made since the raise."""
    from plans.public import PlanError, get_node

    row = db.execute(
        """SELECT delivered_node_id FROM doc_feedback_batches
            WHERE author_kind='user' AND author_id=? AND artifact_id=?
              AND delivery_outcome='node' AND updated_at>=?
            ORDER BY updated_at DESC, id DESC LIMIT 1""",
        (USER_AUTHOR_ID, artifact_id, since or ""),
    ).fetchone()
    if row is None or not row["delivered_node_id"]:
        return None
    try:
        return get_node(str(row["delivered_node_id"]), db=db).title
    except PlanError:
        return None


def round_state(db, artifact_id: str) -> dict:
    """The review bar's read: the newest raised ask, the other askers and the round's feedback."""
    from plans.public import raised_asks

    asks = raised_asks(db, artifact_id)
    newest = asks[0] if asks else None
    counts = round_counts(db, artifact_id, newest["raised_at"] if newest else None)
    seen = {newest["asker_id"]} if newest else set()
    others, other_asks = [], []
    for ask in asks[1:]:
        if ask["asker_id"] not in seen:
            seen.add(ask["asker_id"])
            others.append(ask["asker_name"] or str(ask["asker_id"]))
            other_asks.append({"name": others[-1], "reason": ask["reason"]})
    return {
        "ask_raised": newest is not None,
        "hold_ids": [str(ask["hold_id"]) for ask in asks],
        "ask_reason": newest["reason"] if newest else None,
        "ask_raised_at": newest["raised_at"] if newest else None,
        "asker": (
            {"id": newest["asker_id"], "name": newest["asker_name"] or str(newest["asker_id"])}
            if newest else None
        ),
        "other_askers": others,
        "other_asks": other_asks,
        "other_request_count": len(asks) - 1 if asks else 0,
        "round_feedback_count": sum(counts.values()),
        "round_feedback_counts": counts,
    }


def answer(
    db, artifact_id: str, verdict: str, note: str | None,
    hold_ids: list[str] | None = None,
) -> ReviewAnswer:
    """Check the verdict against the round, then close the user's open feedback batch.

    ``hold_ids`` are the asks the person's bar showed (``round_state``); when
    the newest open ask is not among them, it replaced what they read, so the
    verdict is refused as stale rather than applied to a request they never saw.

    Runs in the caller's write transaction, before the asks are released, so
    the feedback task exists when the release notice points at it.
    """
    from plans.public import raised_asks

    if verdict not in VERDICTS:
        raise AnswerRefused("verdict_unknown", f"unknown verdict {verdict!r}")
    note = (note or "").strip() or None
    if note and len(note) > NOTE_MAX:
        raise AnswerRefused("note_too_long", f"The note is over {NOTE_MAX} characters.")
    asks = raised_asks(db, artifact_id)
    if not asks or (hold_ids is not None and str(asks[0]["hold_id"]) not in hold_ids):
        raise AnswerRefused("ask_stale", STALE)
    since = asks[0]["raised_at"]
    counts = round_counts(db, artifact_id, since)
    if verdict == "changes_requested" and not note and not any(counts.values()):
        raise AnswerRefused("note_required", NOTE_REQUIRED)
    # The round decides which approval it was, whatever the client last showed.
    if verdict in ("approved", "approved_with_changes"):
        verdict = "approved_with_changes" if any(counts.values()) else "approved"
    feedback_batches.close_for_document(
        db, author_kind="user", author_id=USER_AUTHOR_ID,
        artifact_id=artifact_id, reason="send",
    )
    return ReviewAnswer(verdict, note, counts, _feedback_task(db, artifact_id, since))
