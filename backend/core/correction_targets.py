"""The one rule for which agent a document-comment correction is aimed at.

A comment correction targets the agent that was handed the person's feedback:
the recipient of a feedback batch the comment joined, which every user comment
does as it is written. A reply to an agent's comment joins two batches, one to
the document's owner and one to that comment's author; the owner's wins. When
the owner changed after the comment was handed out, the newest batch's
recipient wins, because that agent got the feedback. A comment in no batch yet
targets the document's current owner registration; with none it has no target.

The scan's eligibility, the fact's ``thread_id`` and ``agent_thread_id``, and
the note's recipient all read this one expression, so they cannot disagree.
"""

from __future__ import annotations

from registry.identity_bridge import OWNER_REGISTRATION_STATES

_OWNER_STATES = ",".join(f"'{state}'" for state in OWNER_REGISTRATION_STATES)


def _owner_sql(comment: str) -> str:
    """The document's owner registration, pinned first then newest, as ``registry`` resolves it."""
    return f"""(SELECT r.parent_thread_id FROM artifact_registrations r
                 WHERE r.artifact_id={comment}.artifact_id AND r.relationship_state IN ({_OWNER_STATES})
                 ORDER BY r.relationship_state<>'owner-pinned', r.updated_at DESC, r.id LIMIT 1)"""


def comment_target_sql(comment: str = "c") -> str:
    """SQL for the target thread of the ``doc_comments`` row aliased ``comment``, or NULL."""
    owner = _owner_sql(comment)
    # SQLite cannot read an outer column in a subquery's ORDER BY, so "the owner's
    # batch first" is its own lookup rather than a sort key.
    batches = f"""FROM doc_feedback_batch_items i JOIN doc_feedback_batches b ON b.id=i.batch_id
          WHERE i.comment_id={comment}.id AND i.event IS NULL AND b.author_kind='user'"""
    return f"""COALESCE(
        (SELECT b.recipient_id {batches} AND b.recipient_id={owner} LIMIT 1),
        (SELECT b.recipient_id {batches} ORDER BY b.created_at DESC, b.id DESC LIMIT 1),
        {owner})"""

