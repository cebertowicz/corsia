"""An edit by anyone else that strands a pending agent suggestion tells its author, once."""

from __future__ import annotations

import hashlib
import json

import pytest

from doceditor import comment_actions, comments, comments as store
from infra.database import write_transaction
from registry import service
from tests.test_shared_document_ownership import _write, ownership_scope as ownership_scope  # noqa: F401

ORIGINAL = "alpha beta gamma\n"
CHANGED = "alpha changed gamma\n"


class _Doc:
    def __init__(self, scope):
        self.scope, self.db, self.name = scope, scope["db"], "doc.md"
        self.path = scope["category"] / self.name
        self.author = scope["other"]
        _write(scope, self.name, ORIGINAL)
        self.artifact_id = service.find_file_identity(str(self.path), db=self.db)["id"]

    def suggest(self, *, kind="agent", thread=None, replacement="BETA") -> dict:
        row, _ = store.create_with_status(
            self.artifact_id, body="Use this wording.", author="Other Lane", author_kind=kind,
            author_thread_id=(thread or self.author) if kind == "agent" else None,
            anchor_text="beta", from_off=6, to_off=10, anchor_prefix="alpha ", anchor_suffix=" gamma\n",
            replacement=replacement, db=self.db,
        )
        self.db.commit()
        return row

    def save(self, text: str, *, actor: str | None = None) -> None:
        """One registered content edit by ``actor`` (a thread id, or the person as "ui")."""
        self.path.write_text(text)
        service.register_write_strict(
            self.scope["owner"], str(self.path),
            metadata={"last_written_sha256": hashlib.sha256(text.encode()).hexdigest()},
            actor_thread_id=actor or self.scope["owner"], db=self.db,
        )

    def notes(self, thread: str | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT summary, details_json FROM context_change_journal "
            "WHERE object_kind='thread_context' AND object_id=? ORDER BY seq", (thread or self.author,),
        ).fetchall()
        return [{"summary": row["summary"], "details": json.loads(row["details_json"])} for row in rows
                if json.loads(row["details_json"]).get("kind") == "suggestion_stranded"]


@pytest.fixture
def doc(ownership_scope):  # noqa: F811
    return _Doc(ownership_scope)


def test_an_edit_by_another_writer_tells_the_author_once_while_it_stays_stranded(doc):
    suggestion = doc.suggest()

    doc.save(CHANGED)
    doc.save("alpha changed again gamma\n")

    [note] = doc.notes()
    assert note["summary"] == (
        f"Documents changed the text your suggestion replaces on doc, so it can no longer be applied: "
        f"“beta” ({suggestion['id']}). If it still applies, suggest again on the current text."
    )
    assert note["details"]["artifact_id"] == doc.artifact_id
    assert note["details"]["comment_id"] == suggestion["id"]
    assert note["details"]["chip"] == "Documents's edit stranded a suggestion on doc."


def test_the_person_is_named_when_they_made_the_edit(doc):
    doc.suggest()

    doc.save(CHANGED, actor="ui")

    assert doc.notes()[0]["summary"].startswith("The user changed the text your suggestion replaces on doc")


def test_an_edit_that_leaves_the_text_in_place_tells_no_one(doc):
    doc.suggest()

    doc.save("A new heading\n\n" + ORIGINAL)

    assert doc.notes() == []


def test_the_authors_own_edit_tells_no_one_and_no_later_save_does_either(doc):
    doc.suggest()

    doc.save(CHANGED, actor=doc.author)
    doc.save("alpha changed again gamma\n")

    assert doc.notes() == []


def test_a_user_authored_suggestion_tells_no_one_and_is_not_marked(doc):
    suggestion = doc.suggest(kind="user")

    doc.save(CHANGED)

    assert doc.db.execute("SELECT COUNT(*) FROM context_change_journal WHERE object_kind='thread_context' "
                          "AND details_json LIKE '%suggestion_stranded%'").fetchone()[0] == 0
    assert doc.db.execute("SELECT stranded_notified_at FROM doc_comments WHERE id=?",
                          (suggestion["id"],)).fetchone()[0] is None


def test_a_decided_suggestion_tells_no_one(doc):
    suggestion = doc.suggest()
    with write_transaction(doc.db, scope="test_stranding_decision") as connection:
        comment_actions.record_decision(
            suggestion["id"], decision="reject", decided_by="Robin", decided_by_thread_id=None, db=connection,
        )

    doc.save(CHANGED)

    assert doc.notes() == []


def test_a_resolved_suggestion_tells_no_one(doc):
    suggestion = doc.suggest()
    comments.update(suggestion["id"], {"resolved": True}, db=doc.db)

    doc.save(CHANGED)

    assert doc.notes() == []


def test_an_author_whose_thread_is_gone_is_not_told(doc):
    doc.suggest(thread="deleted-thread")

    doc.save(CHANGED)

    assert doc.notes("deleted-thread") == []


def test_accepting_the_suggestion_does_not_strand_it(doc):
    doc.suggest()

    doc.save("alpha BETA gamma\n", actor="ui")

    assert doc.notes() == []


def test_accepting_a_deletion_does_not_strand_it(doc):
    doc.suggest(replacement="")

    doc.save("alpha  gamma\n", actor="ui")

    assert doc.notes() == []


def test_a_suggestion_that_anchors_again_is_told_again_when_stranded_again(doc):
    doc.suggest()

    doc.save(CHANGED)
    doc.save(ORIGINAL)
    doc.save("alpha changed once more gamma\n")

    assert len(doc.notes()) == 2


def test_reading_the_comments_never_sends_the_note(doc):
    from doceditor.comment_anchoring import anchored_threads

    doc.suggest()
    doc.path.write_text(CHANGED)

    anchored_threads(doc.artifact_id, str(doc.path), doc.db)

    assert doc.notes() == []
