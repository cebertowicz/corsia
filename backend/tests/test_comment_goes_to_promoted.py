"""A pane on a promoted document's old id names the agents of the live document in its 'Goes to' line.

Since #2982 the comment list is read under the live identity. The recipients the box names are
resolved from that same document, so they are the agents delivery wakes for a comment written there.
"""

from __future__ import annotations

from doc_feedback_helpers import seed_thread
from doceditor import feedback_comments
from infra.database import get_db
from registry import models
from test_old_id_comment_writes import TOKEN, _document_with_thread, _promote, client  # noqa: F401


def test_a_pane_on_the_old_id_names_the_live_documents_agent(client, isolated_db):
    db = get_db()
    try:
        artifact, _root = _document_with_thread(db)
        seed_thread(db, "successor-owner", title="Successor owner")
        db.execute("UPDATE threads SET user_id=(SELECT user_id FROM threads WHERE id='owner') WHERE id='successor-owner'")
        successor = _promote(db, artifact, owner="successor-owner")
        response = client.get(f"/api/artifacts/{artifact}/comments", params=TOKEN)
        assert response.status_code == 200, response.text
        named = [entry["id"] for entry in response.json()["feedback"]["goes_to"]]
        woken = [who for who in feedback_comments.artifact_recipients(db, models.get_artifact(successor, db=db)) if who]
        assert named == woken
        assert "successor-owner" in named
        assert "owner" not in named
    finally:
        db.close()
