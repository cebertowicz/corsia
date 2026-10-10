"""DCOL-FR10.2: a reply to an agent's comment reaches that agent's plan once the batch is sent."""

from __future__ import annotations

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db


@pytest.fixture
def scene(isolated_db):
    db = get_db()
    for thread_id in ("owner", "root-author", "replier"):
        seed_thread(db, thread_id)
    artifact_id = seed_document(db, "owner")
    root, _ = add_comment(db, artifact_id, author_id="root-author", author="Root")
    yield db, artifact_id, root
    db.close()


def _delivered_node(db, recipient):
    row = next(batch for batch in batches(db) if batch["recipient_id"] == recipient and batch["state"] == "delivered")
    assert row["delivery_outcome"] == "node" and row["delivered_node_id"] is not None
    return row["delivered_node_id"]


def test_an_agents_reply_becomes_a_feedback_node_on_the_comment_authors_plan(scene):
    db, artifact_id, root = scene
    add_comment(db, artifact_id, author_id="replier", author="Replier", parent_id=root["id"])
    batch = next(row for row in batches(db) if row["recipient_id"] == "root-author" and row["author_id"] == "replier")

    feedback_batches.close_batch(db, batch["id"], "quiet")

    node = db.execute("SELECT * FROM plan_nodes WHERE id=?", (_delivered_node(db, "root-author"),)).fetchone()
    assert node is not None


def test_a_users_reply_becomes_a_feedback_node_on_the_comment_authors_plan(scene):
    db, artifact_id, root = scene
    add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve", parent_id=root["id"])

    feedback_batches.close_for_document(
        db, author_kind="user", author_id=feedback_batches.USER_AUTHOR_ID, artifact_id=artifact_id, reason="send",
    )

    assert db.execute("SELECT * FROM plan_nodes WHERE id=?", (_delivered_node(db, "root-author"),)).fetchone()
