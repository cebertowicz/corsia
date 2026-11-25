"""A comment task lands under the task its document belongs to, never on top.

Plan order is priority and the newest thing is not first: a task opened by
comments on a document sits right after the task that owns the document, with
the person's comment tasks before another agent's at that spot.
"""

from __future__ import annotations

from doc_feedback_helpers import add_comment, add_person_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db
from plans import artifact_links, service


def _top(db, plan_id: str) -> list[str]:
    return [
        str(row["title"]) for row in db.execute(
            "SELECT title FROM plan_nodes WHERE plan_id=? AND parent_id IS NULL "
            "AND archived_at IS NULL ORDER BY order_key, id",
            (plan_id,),
        ).fetchall()
    ]


def _task(plan_id: str, title: str, db) -> str:
    node = service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria="done",
        intent=title, actor_id=None, db=db,
    )
    return node.id


def _owned_by(db, artifact_id: str, node_id: str) -> None:
    db.execute(
        "UPDATE artifact_registrations SET origin_plan_node_id=? WHERE artifact_id=? AND retired_at IS NULL",
        (node_id, artifact_id),
    )
    db.commit()


def _agent_batch(db, artifact_id: str) -> str:
    add_comment(db, artifact_id)
    batch = batches(db, state="open")[0]
    feedback_batches.close_batch(db, batch["id"], "quiet")
    return str(next(b for b in batches(db) if b["id"] == batch["id"])["delivered_node_id"])


def test_comment_task_lands_after_owning_task_not_at_the_end(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        plan = service.ensure_plan("thread", "owner", db=db)
        _task(plan.id, "First", db)
        writing = _task(plan.id, "Write the page", db)
        _task(plan.id, "Third", db)
        artifact_id = seed_document(db, "owner", name="page.md")
        _owned_by(db, artifact_id, writing)

        node_id = _agent_batch(db, artifact_id)
        title = service.get_node(node_id, db=db).title

        assert _top(db, plan.id) == ["First", "Write the page", title, "Third"]
    finally:
        db.close()


def test_result_link_owns_when_registration_names_no_task(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        plan = service.ensure_plan("thread", "owner", db=db)
        writing = _task(plan.id, "Write the page", db)
        _task(plan.id, "Later work", db)
        artifact_id = seed_document(db, "owner", name="page.md")
        artifact_links.link(writing, artifact_id, "result", actor_id=None, provenance="test", db=db)
        db.commit()

        node_id = _agent_batch(db, artifact_id)
        title = service.get_node(node_id, db=db).title

        assert _top(db, plan.id) == ["Write the page", title, "Later work"]
    finally:
        db.close()


def test_person_comment_task_sits_above_agent_comment_task_at_the_spot(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        plan = service.ensure_plan("thread", "owner", db=db)
        writing = _task(plan.id, "Write the page", db)
        _task(plan.id, "Later work", db)
        # Comments on one document join one task, so the two kinds meet at a
        # spot through two documents the same task owns.
        reviewed = seed_document(db, "owner", name="page.md")
        noted = seed_document(db, "owner", name="notes.md")
        _owned_by(db, reviewed, writing)
        _owned_by(db, noted, writing)

        agent_node = _agent_batch(db, reviewed)
        add_person_comment(db, noted)
        person_node = next(b for b in batches(db) if b["author_kind"] == "user")["delivered_node_id"]
        agent_title = service.get_node(agent_node, db=db).title
        person_title = service.get_node(person_node, db=db).title

        assert _top(db, plan.id) == ["Write the page", person_title, agent_title, "Later work"]
    finally:
        db.close()


def test_no_owning_task_appends_as_before(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        plan = service.ensure_plan("thread", "owner", db=db)
        _task(plan.id, "First", db)
        _task(plan.id, "Second", db)
        artifact_id = seed_document(db, "owner", name="page.md")

        node_id = _agent_batch(db, artifact_id)
        title = service.get_node(node_id, db=db).title

        assert _top(db, plan.id) == ["First", "Second", title]
    finally:
        db.close()
