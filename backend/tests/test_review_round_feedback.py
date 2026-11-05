"""Round feedback IDs and counts share the rows used by review answers."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from doceditor import feedback_batch_edits, feedback_batches
from doceditor.feedback_state import USER_AUTHOR_ID
from doceditor.review_round_feedback import round_feedback
from infra.database import write_transaction
from doc_feedback_helpers import seed_document
from test_review_bar_answer import db, _ask, _comment


def test_round_feedback_counts_and_ids_use_the_same_rows(db):
    artifact = seed_document(db, "owner", name="round.md")
    _ask(db, artifact)
    _comment(db, artifact)
    _comment(db, artifact, replacement="better")
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()

    def edit(after):
        with write_transaction(db, scope="test_round_edit") as connection:
            return feedback_batch_edits.record_edit(
                connection, artifact_id=artifact, recipient_id="owner", author_kind="user",
                author_id=USER_AUTHOR_ID, actor_display="Steve", path=Path("/workspace/shared/round.md"),
                before=b"before\n", after=after, at=later,
            )

    sent = edit(b"sent\n")
    delivered, _ = feedback_batches.close_batch(db, sent, "send")
    assert delivered
    db.commit()
    opened = edit(b"open\n")
    rows = db.execute("SELECT id, replacement FROM doc_comments WHERE artifact_id=?", (artifact,)).fetchall()
    counts, ids = round_feedback(db, artifact, None)
    assert counts == {"comments": 1, "suggestions": 1, "edits": 2}
    assert ids == {
        "comments": [row["id"] for row in rows if row["replacement"] is None],
        "suggestions": [row["id"] for row in rows if row["replacement"] is not None],
        "edits": sorted([sent, opened]),
    }
    db.execute("UPDATE doc_feedback_batches SET item_count=item_count+2 WHERE id=?", (sent,))
    counts, ids = round_feedback(db, artifact, None)
    assert counts["edits"] == 4
    assert ids["edits"] == sorted([sent, opened])
