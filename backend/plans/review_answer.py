"""The user's verdict on an artifact an agent asked them to review (DCOL-FR12.4).

One round is one user hold: it runs from the ask's raise to the user's answer.
The round's feedback is what the user authored on the artifact after the raise.
Answering closes the user's open feedback batch through the same writer as
send-on-close, so the comments reach the owner once as the usual feedback task,
and then releases every raised ask on the artifact with the verdict, the note
and a pointer to that task. ``shared/documents/DRAFT-review-bar-design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VERDICTS = {
    "approved": "approved",
    "approved_with_changes": "approved with changes",
    "changes_requested": "requested changes on",
}
NOTE_MAX = 2000
NOTE_REQUIRED = "Say what should change: this round has no comments, suggestions or edits."
STALE = "This review request was replaced."


class AnswerRefused(ValueError):
    """The verdict cannot be recorded; ``code`` is the typed reason a client branches on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Answer:
    """What the user decided, as the release notice and the clearance event carry it."""

    verdict: str
    note: str | None = None
    counts: dict = field(default_factory=dict)
    feedback_task: str | None = None

    def payload(self) -> dict:
        return {
            "verdict": self.verdict, "note": self.note,
            "feedback_counts": self.counts,
        }


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def counts_phrase(counts: dict) -> str:
    """``2 comments, 1 suggestion`` for the kinds the round holds, in a fixed order."""
    return ", ".join(
        _plural(counts[kind], kind[:-1]) for kind in ("comments", "suggestions", "edits")
        if counts.get(kind)
    )


def release_summary(answer: Answer, *, ask: str, kind: str, title: str) -> str:
    """The notice an asking agent wakes on (design D5)."""
    detail = counts_phrase(answer.counts)
    if answer.feedback_task:
        detail = f'{detail + ", " if detail else ""}in task "{answer.feedback_task}"'
    text = (
        f'The user {VERDICTS[answer.verdict]} "{ask}"'
        f'{f" ({detail})" if detail else ""}, so {kind} "{title}" '
        "no longer waits on them: pick it up."
    )
    return f"{text}\nTheir note: {answer.note}" if answer.note else text


def raised_holds(db, artifact_id: str) -> list:
    """The user holds that wait on this artifact, newest first, with their asking agent."""
    return db.execute(
        """SELECT h.id AS hold_id, h.reason, h.created_at AS raised_at,
                  n.id AS node_id, p.owner_id AS asker_id, t.title AS asker_name
             FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id = l.node_id
             JOIN plans p ON p.id = n.plan_id
             JOIN plan_node_holds h ON h.node_id = n.id
             LEFT JOIN threads t ON t.id = p.owner_id AND p.owner_kind = 'thread'
            WHERE l.artifact_id=? AND l.role='attention'
              AND h.kind='blocked' AND h.owner_kind='user'
              AND h.owner_id=l.artifact_id AND h.cleared_at IS NULL
            ORDER BY h.created_at DESC, h.id DESC""",
        (artifact_id,),
    ).fetchall()


def round_counts(db, artifact_id: str, since: str | None) -> dict:
    """The comments, suggestions and edits the user made on the artifact since ``since``."""
    from doceditor.feedback_state import USER_AUTHOR_ID

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
    from doceditor.feedback_state import USER_AUTHOR_ID

    row = db.execute(
        """SELECT n.title FROM doc_feedback_batches b
             JOIN plan_nodes n ON n.id=b.delivered_node_id
            WHERE b.author_kind='user' AND b.author_id=? AND b.artifact_id=?
              AND b.delivery_outcome='node' AND b.updated_at>=?
            ORDER BY b.updated_at DESC, b.id DESC LIMIT 1""",
        (USER_AUTHOR_ID, artifact_id, since or ""),
    ).fetchone()
    return str(row["title"]) if row else None


def round_state(db, artifact_id: str) -> dict:
    """The review bar's read: the newest raised ask, the other askers and the round's feedback."""
    holds = raised_holds(db, artifact_id)
    counts = round_counts(db, artifact_id, holds[0]["raised_at"] if holds else None)
    newest = holds[0] if holds else None
    seen = {newest["asker_id"]} if newest else set()
    others, other_asks = [], []
    for hold in holds[1:]:
        if hold["asker_id"] not in seen:
            seen.add(hold["asker_id"])
            others.append(hold["asker_name"] or str(hold["asker_id"]))
            other_asks.append({"name": others[-1], "reason": hold["reason"]})
    return {
        "ask_raised": newest is not None,
        "ask_reason": newest["reason"] if newest else None,
        "ask_raised_at": newest["raised_at"] if newest else None,
        "asker": (
            {"id": newest["asker_id"], "name": newest["asker_name"] or str(newest["asker_id"])}
            if newest else None
        ),
        "other_askers": others,
        "other_asks": other_asks,
        "other_request_count": len(holds) - 1 if holds else 0,
        "round_feedback_count": sum(counts.values()),
        "round_feedback_counts": counts,
    }


def answer(db, identity_id: str, verdict: str, note: str | None) -> Answer:
    """Check the verdict against the round, then close the user's open feedback batch.

    Runs in the caller's write transaction, before the asks are released, so
    the feedback task exists when the release notice points at it.
    """
    from doceditor import feedback_batches
    from doceditor.feedback_state import USER_AUTHOR_ID

    if verdict not in VERDICTS:
        raise AnswerRefused("verdict_unknown", f"unknown verdict {verdict!r}")
    note = (note or "").strip() or None
    if note and len(note) > NOTE_MAX:
        raise AnswerRefused("note_too_long", f"The note is over {NOTE_MAX} characters.")
    holds = raised_holds(db, identity_id)
    if not holds:
        raise AnswerRefused("ask_stale", STALE)
    since = holds[0]["raised_at"]
    counts = round_counts(db, identity_id, since)
    if verdict == "changes_requested" and not note and not any(counts.values()):
        raise AnswerRefused("note_required", NOTE_REQUIRED)
    # The round decides which approval it was, whatever the client last showed.
    if verdict in ("approved", "approved_with_changes"):
        verdict = "approved_with_changes" if any(counts.values()) else "approved"
    feedback_batches.close_for_document(
        db, author_kind="user", author_id=USER_AUTHOR_ID,
        artifact_id=identity_id, reason="send",
    )
    return Answer(verdict, note, counts, _feedback_task(db, identity_id, since))
