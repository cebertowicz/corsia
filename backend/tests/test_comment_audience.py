"""The reply box names exactly the agents delivery wakes, owner first, then in the order they joined."""

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comment_audience, comment_recipients, feedback_comments
from infra.database import get_db
from registry import models as registry_models


def _annotated(db, artifact, roots):
    comment_audience.annotate(db, artifact, roots)
    return {root["id"]: [entry["id"] for entry in root["reply_goes_to"]] for root in roots}


def test_a_reply_goes_to_the_owner_first_then_agents_in_the_order_they_joined(isolated_db):
    db = get_db()
    for name in ("owner", "first", "second", "third"):
        seed_thread(db, name)
    artifact = registry_models.get_artifact(seed_document(db, "owner"), db=db)
    root, _ = add_comment(db, artifact["id"], author_id="first")
    add_comment(db, artifact["id"], author_id="third", parent_id=root["id"])
    add_comment(db, artifact["id"], author_id="second", parent_id=root["id"])
    named = _annotated(db, artifact, [root])[root["id"]]
    assert named == ["owner", "first", "third", "second"]
    person, _ = add_comment(db, artifact["id"], author_kind="user", author_id=None, parent_id=root["id"])
    assert set(named) == set(feedback_comments.recipients(db, person))


def test_an_agent_that_left_the_chat_is_not_named(isolated_db):
    db = get_db()
    for name in ("owner", "first"):
        seed_thread(db, name)
    artifact = registry_models.get_artifact(seed_document(db, "owner"), db=db)
    root, _ = add_comment(db, artifact["id"], author_id="first")
    comment_recipients.leave(db, root, "first")
    db.commit()
    assert _annotated(db, artifact, [root])[root["id"]] == ["owner"]


def test_an_owner_that_is_not_a_thread_is_not_named(isolated_db):
    db = get_db()
    seed_thread(db, "first")
    artifact = registry_models.get_artifact(seed_document(db, "not-a-thread"), db=db)
    root, _ = add_comment(db, artifact["id"], author_id="first")
    assert _annotated(db, artifact, [root])[root["id"]] == ["first"]


def test_reads_do_not_grow_with_the_number_of_threads(isolated_db):
    db = get_db()
    for name in ("owner", "first", "second"):
        seed_thread(db, name)
    artifact = registry_models.get_artifact(seed_document(db, "owner"), db=db)
    roots = []
    for index in range(12):
        root, _ = add_comment(db, artifact["id"], author_id="first" if index % 2 else "second")
        add_comment(db, artifact["id"], author_id="second", parent_id=root["id"])
        roots.append(root)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        comment_audience.annotate(db, artifact, roots[:1])
        for_one = len(statements)
        statements.clear()
        everyone = _annotated(db, artifact, roots)
        for_all = len(statements)
    finally:
        db.set_trace_callback(None)
    assert for_all == for_one
    for root in roots:
        assert set(everyone[root["id"]]) == set(feedback_comments.recipients(db, {"id": "x", "artifact_id": artifact["id"], "parent_id": root["id"], "author_kind": "user", "tagged_agent_ids": []}))
