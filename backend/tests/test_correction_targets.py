"""One target rule for a comment correction.

The target is the agent that was handed the person's feedback, which can differ
from the document's current owner. The scan, the candidate and the note all
read that one rule.
"""

from __future__ import annotations

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from tests.test_correction_notes import _version  # noqa: F401
from tests.test_corrections_comments_answers import (  # noqa: F401 - shared fixture and helpers
    CORRECTION,
    _correction_notes,
    _deliver_batch,
    _record,
    _user_comment,
    _worker_takes,
    db,
)


def _candidate_thread(source_id: str) -> str | None:
    from jobs.corrections_recording import source_candidate

    candidate = source_candidate("doc_comment", source_id)
    return candidate.thread_id if candidate is not None else None


def _deliver_all(db) -> None:
    from doceditor import feedback_batches

    for batch in batches(db, state="open"):
        assert feedback_batches.close_batch(db, batch["id"], "send")[0]
    _worker_takes(db)


def test_a_reply_to_an_agent_comment_targets_the_owner_not_the_comment_author(db):
    seed_thread(db, "reviewer")
    artifact = seed_document(db, "owner", name="p.md")
    agent_row, _ = add_comment(db, artifact, author_kind="agent", author_id="reviewer")
    db.commit()
    _deliver_all(db)
    earlier = _user_comment(db, artifact)  # opens the owner's batch first, so the reviewer's is newer
    db.execute("UPDATE doc_feedback_batches SET created_at='2000-01-01T00:00:00' WHERE state='open'")
    db.commit()

    reply, _ = add_comment(
        db, artifact, author_kind="user", author="Steve", author_id=None, parent_id=str(agent_row["id"]),
    )
    db.commit()
    newest_first = db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE state='open' ORDER BY created_at DESC, id DESC"
    ).fetchall()
    assert [row[0] for row in newest_first] == ["reviewer", "owner"]  # the reply joined both batches

    assert _candidate_thread(str(reply["id"])) == "owner"
    for comment in (earlier, str(reply["id"])):
        _record(db, "doc_comment", comment)
    _deliver_all(db)
    assert {details["note_key"].split(":")[0] for details, _ in _correction_notes(db)} == {"doc-feedback"}
    owner_notes = db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_id='owner' AND details_json LIKE '%correction_note%'"
    ).fetchone()[0]
    assert owner_notes == 1 and len(_correction_notes(db)) == 1


def test_a_comment_handed_out_before_the_owner_changed_stays_with_its_recipient(db):
    seed_thread(db, "successor")
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    batch = _deliver_batch(db)
    db.execute("UPDATE artifact_registrations SET parent_thread_id='successor' WHERE artifact_id=?", (artifact,))
    db.commit()

    assert _candidate_thread(comment) == "owner"  # the agent that got the feedback, not the new owner
    assert _record(db, "doc_comment", comment) is True
    (details, _summary), = _correction_notes(db)
    assert details["note_key"] == f"doc-feedback:{batch['id']}"


def test_a_comment_in_no_batch_targets_the_current_owner_and_with_none_has_no_target(db):
    artifact = seed_document(db, "owner", name="p.md")
    comment = _user_comment(db, artifact)
    db.execute("DELETE FROM doc_feedback_batch_items WHERE comment_id=?", (comment,))
    db.commit()
    assert _candidate_thread(comment) == "owner"

    db.execute("UPDATE artifact_registrations SET relationship_state='subscriber-recent' WHERE artifact_id=?", (artifact,))
    db.commit()
    assert _candidate_thread(comment) is None


def test_the_scan_selects_exactly_the_comments_that_have_a_candidate(db):
    from jobs.corrections_scanning import pending_source_candidate_keys

    seed_thread(db, "reviewer")
    handed = seed_document(db, "owner", name="handed.md")
    handed_comment = _user_comment(db, handed)
    _deliver_batch(db)
    owned = seed_document(db, "owner", name="owned.md")
    released = seed_document(db, "owner", name="released.md")
    comments = [handed_comment, _user_comment(db, owned), _user_comment(db, released)]
    agent_row, _ = add_comment(db, owned, author_kind="agent", author_id="reviewer")
    # Both documents lose their owner. The handed one's comment still went to an agent;
    # the other's was never handed out, so no agent was or will be given it.
    db.execute("DELETE FROM doc_feedback_batch_items WHERE comment_id=?", (comments[2],))
    db.execute(
        "UPDATE artifact_registrations SET relationship_state='subscriber-recent' WHERE artifact_id IN (?,?)",
        (handed, released),
    )
    db.commit()

    scanned = {key.source_id for key in pending_source_candidate_keys(_version(), 50) if key.source_kind == "doc_comment"}
    hydrated = {source for source in [*comments, str(agent_row["id"])] if _candidate_thread(source) is not None}

    assert scanned == hydrated == {comments[0], comments[1]}
