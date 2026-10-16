"""A document can have only one open review request."""
from __future__ import annotations

import json
import uuid

import pytest

from plans import service, user_ask
from plans.boundary import PlanActor, mutate
from registry import models
from tests.test_attention_queue import db  # noqa: F401 - in-memory plan fixture


OWNER = PlanActor("root", "user-1", "project-1")
USER = PlanActor(None, "user-1", "project-1", kind="user")


def _document(db) -> str:
    models.register_artifact(
        integration="workspace", type="document", external_id="review.md",
        parent_thread_id="root", title="Review", db=db,
    )
    return str(db.execute(
        "SELECT id FROM artifact_identities WHERE external_id='review.md'",
    ).fetchone()["id"])


def _ask_release_wakes(db):
    return db.execute(
        "SELECT id FROM messages WHERE id LIKE 'plan-wake:ask_released:%' ORDER BY input_order",
    ).fetchall()


def _block(
    db, plan_id: str, node_id: str, artifact_id: str, reason: str = "Review it",
    *, expected_revision: int | None = None,
):
    return mutate(
        plan_id, "block_node", {
            "node_id": node_id, "reason_key": "review", "reason": reason,
            "owner_kind": "user", "ask_artifact_id": artifact_id,
            "expected_revision": expected_revision if expected_revision is not None
            else service._node_row(db, node_id)["revision"],
        }, actor=OWNER, provenance="test.one_open_ask", mutation_id=uuid.uuid4().hex, db=db,
    )


def _end(db, plan_id: str, node_id: str, operation: str) -> None:
    params = {"node_id": node_id, "expected_revision": service._node_row(db, node_id)["revision"]}
    if operation == "close_node":
        params["reason"] = "Not needed"
    mutate(
        plan_id, operation, params, actor=USER, provenance="test.one_open_ask",
        mutation_id=uuid.uuid4().hex, db=db,
    )


def test_refuses_competing_asks_and_atomically_replaces_a_new_reason(db):
    plan = service.ensure_plan("thread", "root", db=db)
    first = service.add_node(
        plan.id, kind="task", title="First", acceptance_criteria="Reviewed", db=db,
    )
    second = service.add_node(
        plan.id, kind="task", title="Second", acceptance_criteria="Reviewed", db=db,
    )
    artifact_id = _document(db)
    db.commit()

    _block(db, plan.id, first.id, artifact_id)
    [hold] = db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (first.id,),
    ).fetchall()

    _block(db, plan.id, first.id, artifact_id)
    assert db.execute(
        "SELECT count(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (first.id,),
    ).fetchone()[0] == 1

    with pytest.raises(
        service.PlanInputError, match=rf"node_id {first.id}, hold_id {hold['id']}.*do not resolve it",
    ):
        _block(db, plan.id, second.id, artifact_id)
    stale_revision = service._node_row(db, first.id)["revision"]
    _block(db, plan.id, first.id, artifact_id, reason="Review the revision")
    [replacement] = db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (first.id,),
    ).fetchall()
    assert replacement["id"] != hold["id"]
    previous = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE id=("
        "SELECT clearance_event_id FROM plan_node_holds WHERE id=?)", (hold["id"],),
    ).fetchone()["payload_json"]
    assert json.loads(previous)["detail"] == {"ended": "replaced"}
    assert _ask_release_wakes(db) == []

    with pytest.raises(service.PlanError):
        _block(
            db, plan.id, first.id, artifact_id, reason="A stale replacement",
            expected_revision=stale_revision,
        )
    assert db.execute(
        "SELECT count(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (first.id,),
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT count(*) FROM plan_node_holds WHERE node_id=?", (first.id,),
    ).fetchone()[0] == 2

    mutate(
        plan.id, "resolve_blocker", {
            "node_id": first.id, "hold_id": replacement["id"],
            "expected_revision": service._node_row(db, first.id)["revision"],
        }, actor=OWNER, provenance="test.one_open_ask", mutation_id=uuid.uuid4().hex, db=db,
    )
    assert _ask_release_wakes(db) == []

    _block(db, plan.id, second.id, artifact_id, reason="Review the revision")
    assert db.execute(
        "SELECT count(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (second.id,),
    ).fetchone()[0] == 1


def test_open_ask_artifacts_reads_the_one_open_request_definition(db):
    from plans import public as plans_public

    plan = service.ensure_plan("thread", "root", db=db)
    first = service.add_node(plan.id, kind="task", title="First", acceptance_criteria="Reviewed", db=db)
    second = service.add_node(plan.id, kind="task", title="Second", acceptance_criteria="Reviewed", db=db)
    first_artifact = _document(db)
    models.register_artifact(
        integration="workspace", type="document", external_id="other-review.md",
        parent_thread_id="root", title="Other review", db=db,
    )
    second_artifact = str(db.execute(
        "SELECT id FROM artifact_identities WHERE external_id='other-review.md'",
    ).fetchone()["id"])
    db.commit()
    _block(db, plan.id, first.id, first_artifact)
    _block(db, plan.id, second.id, second_artifact)

    assert {row["artifact_id"] for row in plans_public.open_ask_artifacts(db, first_artifact)} == {
        first_artifact,
    }
    assert {row["artifact_id"] for row in plans_public.open_ask_artifacts(db)} == {
        first_artifact, second_artifact,
    }
    # The bar's and the rule's read agrees with the pile's on every request.
    for artifact_id in (first_artifact, second_artifact):
        assert {row["hold_id"] for row in user_ask.raised_asks(db, artifact_id)} == {
            row["hold_id"] for row in plans_public.open_ask_artifacts(db, artifact_id)
        }


@pytest.mark.parametrize("operation", ("close_node", "archive_node"))
def test_user_terminal_actions_close_an_open_ask_card(db, operation):
    from registry import attention
    from registry.chat_cards import ask_cards

    plan = service.ensure_plan("thread", "root", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Review", acceptance_criteria="Reviewed", db=db,
    )
    artifact_id = _document(db)
    db.commit()
    _block(db, plan.id, node.id, artifact_id)
    [hold] = db.execute(
        "SELECT id FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (node.id,),
    ).fetchall()

    _end(db, plan.id, node.id, operation)

    payload = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE id=(SELECT clearance_event_id FROM plan_node_holds WHERE id=?)",
        (hold["id"],),
    ).fetchone()["payload_json"]
    assert json.loads(payload)["detail"] == {"ended": "closed"}
    assert ask_cards(db, {hold["id"]: artifact_id})[hold["id"]]["state"] == "closed"
    assert attention.list_raised(db=db) == []
    assert _ask_release_wakes(db) == []


@pytest.mark.parametrize("operation", ("close_node", "archive_node"))
def test_an_ended_tasks_request_frees_the_document_for_another_task(db, operation):
    """Ending a task withdraws its request, so it never blocks a new ask on the document."""
    plan = service.ensure_plan("thread", "root", db=db)
    ended = service.add_node(
        plan.id, kind="task", title="Old", acceptance_criteria="Reviewed", db=db,
    )
    fresh = service.add_node(
        plan.id, kind="task", title="New", acceptance_criteria="Reviewed", db=db,
    )
    artifact_id = _document(db)
    db.commit()
    _block(db, plan.id, ended.id, artifact_id)

    _end(db, plan.id, ended.id, operation)
    assert user_ask.raised_asks(db, artifact_id) == []

    _block(db, plan.id, fresh.id, artifact_id)
    assert [ask["node_id"] for ask in user_ask.raised_asks(db, artifact_id)] == [fresh.id]
