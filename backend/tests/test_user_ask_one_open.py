"""A document can have only one open review request."""
from __future__ import annotations

import json
import uuid

import pytest

from plans import service
from plans.boundary import PlanActor, mutate
from registry import models
from tests.test_attention_queue import db  # noqa: F401 - in-memory plan fixture


OWNER = PlanActor("root", "user-1", "project-1")


def _document(db) -> str:
    models.register_artifact(
        integration="workspace", type="document", external_id="review.md",
        parent_thread_id="root", title="Review", db=db,
    )
    return str(db.execute(
        "SELECT id FROM artifact_identities WHERE external_id='review.md'",
    ).fetchone()["id"])


def _block(db, plan_id: str, node_id: str, artifact_id: str, reason: str = "Review it"):
    return mutate(
        plan_id, "block_node", {
            "node_id": node_id, "reason_key": "review", "reason": reason,
            "owner_kind": "user", "ask_artifact_id": artifact_id,
            "expected_revision": service._node_row(db, node_id)["revision"],
        }, actor=OWNER, provenance="test.one_open_ask", mutation_id=uuid.uuid4().hex, db=db,
    )


def test_refuses_competing_or_restated_asks_until_the_open_hold_is_resolved(db):
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

    with pytest.raises(service.PlanInputError, match=rf"node_id {first.id}.*hold_id {hold['id']}"):
        _block(db, plan.id, second.id, artifact_id)
    with pytest.raises(service.PlanInputError, match=rf"hold_id {hold['id']}.*resolve hold first"):
        _block(db, plan.id, first.id, artifact_id, reason="Review the revision")

    mutate(
        plan.id, "resolve_blocker", {
            "node_id": first.id, "hold_id": hold["id"],
            "expected_revision": service._node_row(db, first.id)["revision"],
        }, actor=OWNER, provenance="test.one_open_ask", mutation_id=uuid.uuid4().hex, db=db,
    )

    _block(db, plan.id, second.id, artifact_id, reason="Review the revision")
    assert db.execute(
        "SELECT count(*) FROM plan_node_holds WHERE node_id=? AND cleared_at IS NULL", (second.id,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("operation", ("close_node", "archive_node"))
def test_user_terminal_actions_withdraw_an_open_ask_card(db, operation):
    from registry.chat_cards import ask_cards

    user = PlanActor(None, "user-1", "project-1", kind="user")
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
    params = {"node_id": node.id, "expected_revision": service._node_row(db, node.id)["revision"]}
    if operation == "close_node":
        params["reason"] = "Not needed"

    mutate(
        plan.id, operation, params, actor=user, provenance="test.one_open_ask",
        mutation_id=uuid.uuid4().hex, db=db,
    )

    payload = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE id=(SELECT clearance_event_id FROM plan_node_holds WHERE id=?)",
        (hold["id"],),
    ).fetchone()["payload_json"]
    assert json.loads(payload)["detail"] == {"ended": "withdrawn"}
    assert ask_cards(db, {hold["id"]: artifact_id})[hold["id"]]["state"] == "withdrawn"
