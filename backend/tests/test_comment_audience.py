"""The comment box names exactly the agents delivery wakes, owner first, then in the order they joined."""

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comment_audience, comment_recipients, feedback_comments
from infra.database import get_db
from registry import models as registry_models


def _ids(named):
    return [entry["id"] for entry in named]


def test_a_new_comment_goes_to_the_owner_delivery_resolves(isolated_db):
    db = get_db()
    seed_thread(db, "owner", title="Docs lane")
    artifact = registry_models.get_artifact(seed_document(db, "owner"), db=db)
    stand_in = {"id": "x", "artifact_id": artifact["id"], "parent_id": None, "author_kind": "user", "tagged_agent_ids": []}
    assert _ids(comment_audience.goes_to(db, artifact)) == list(feedback_comments.recipients(db, stand_in)) == ["owner"]
    assert comment_audience.for_new_comment(db, artifact) == {"state": "agent", "id": "owner", "name": "Docs lane"}


def test_no_owner_thread_means_no_one_is_named(isolated_db):
    db = get_db()
    artifact = registry_models.get_artifact(seed_document(db, "not-a-thread"), db=db)
    assert comment_audience.for_new_comment(db, artifact) == {"state": "none"}


def test_a_reply_goes_to_the_owner_first_then_agents_in_the_order_they_joined(isolated_db):
    db = get_db()
    for name in ("owner", "first", "second", "third"):
        seed_thread(db, name)
    artifact = registry_models.get_artifact(seed_document(db, "owner"), db=db)
    root, _ = add_comment(db, artifact["id"], author_id="first")
    add_comment(db, artifact["id"], author_id="third", parent_id=root["id"])
    add_comment(db, artifact["id"], author_id="second", parent_id=root["id"])
    named = comment_audience.goes_to(db, artifact, root)
    assert _ids(named) == ["owner", "first", "third", "second"]
    person, _ = add_comment(db, artifact["id"], author_kind="user", author_id=None, parent_id=root["id"])
    assert set(_ids(named)) == set(feedback_comments.recipients(db, person))


def test_an_agent_that_left_the_chat_is_not_named(isolated_db):
    db = get_db()
    for name in ("owner", "first"):
        seed_thread(db, name)
    artifact = registry_models.get_artifact(seed_document(db, "owner"), db=db)
    root, _ = add_comment(db, artifact["id"], author_id="first")
    comment_recipients.leave(db, root, "first")
    db.commit()
    assert _ids(comment_audience.goes_to(db, artifact, root)) == ["owner"]
