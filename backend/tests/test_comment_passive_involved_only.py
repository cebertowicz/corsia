"""A comment reaches only agents on its thread; a pane registration alone earns no comment line."""

from __future__ import annotations

import json

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from infra.database import get_db
from infra.passive_changes import materialize_pending_digest
from registry import models


@pytest.fixture
def env(isolated_db, monkeypatch):
    monkeypatch.setattr("infra.passive_changes.clock_event", lambda *_args: None)
    db = get_db()
    for tid in ("owner", "writer", "replier", "pane"):
        seed_thread(db, tid)
    aid = seed_document(db, "owner", name="PRD.md")
    # Thread "pane" merely had the document open: a follower registration.
    db.execute(
        "INSERT INTO artifact_registrations "
        "(id,artifact_id,parent_thread_id,relationship_state,tmp,surfacing,created_at,updated_at) "
        "VALUES ('pane-reg',?,?,'subscriber-recent',0,'visible','t','t')",
        (aid, "pane"),
    )
    db.commit()
    yield db, aid
    db.close()


def lines(db, thread_id: str) -> list[str]:
    accepted = materialize_pending_digest(db, thread_id)
    db.commit()
    if accepted is None:
        return []
    row = db.execute("SELECT metadata FROM messages WHERE id=?", (accepted.message_id,)).fetchone()
    return [e["summary"] for e in json.loads(row["metadata"])["context_events"]
            if e.get("object_kind") == "artifact"]


def test_pane_registration_alone_gets_no_comment_line(env):
    db, aid = env
    root, _ = add_comment(db, aid, author_id="writer")
    assert lines(db, "pane") == []
    # The owner is woken for it through a feedback batch, not a second passive line.
    assert any(b["recipient_id"] == "owner" for b in batches(db))
    assert lines(db, "owner") == []


def test_agents_on_the_thread_still_get_their_wake(env):
    db, aid = env
    root, _ = add_comment(db, aid, author_id="writer")
    add_comment(db, aid, author_id="replier", parent_id=root["id"])
    recipients = {b["recipient_id"] for b in batches(db)}
    assert {"owner", "writer"} <= recipients
    assert "pane" not in recipients


def test_non_comment_artifact_change_still_reaches_pane_thread(env):
    db, aid = env
    models.record_artifact_change(
        artifact_id=aid, internal_event="relocated", external_id=None,
        summary="PRD.md moved.", provenance={"source": "test"}, db=db,
    )
    db.commit()
    assert lines(db, "pane") == ["PRD.md moved."]
