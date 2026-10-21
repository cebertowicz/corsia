"""Delivery proof for every artifact-registration retirement mutation seam."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.database import get_db
from plans import notifications, service
from registry import models, pinning
from registry.node_retirement import after_commit, capture_event_floor
from registry.registration_writer import RegistrationOrigin
from tests.plan_review_helpers import submit_review_evidence
from tests.registry_helpers import make_session
from tests.test_plan_decisions import _DbProxy, _immediate
from tests.test_plan_decision_answer import _client, _raised_decision, db  # noqa: F401


def _node_and_artifact(db, operation: str):
    thread_id = make_session(db, prefix=f"retire-{operation}")
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title=f"{operation} artifact",
        acceptance_criteria="The artifact lifecycle settles",
        db=db,
    )
    node = service.transition_node(node.id, "active", db=db)
    turn_id = f"turn-{operation}-{thread_id}"
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,0,'native/test','running','t0','boot',?,?,?,?,?)""",
        (
            turn_id,
            thread_id,
            plan.id,
            node.id,
            f"attempt-{turn_id}",
            node.commitment_id,
            "execution",
        ),
    )
    db.commit()
    artifact = models.register_artifact(
        integration="workspace",
        type="document",
        external_id=f"{operation}-{thread_id}.md",
        parent_thread_id=thread_id,
        title=f"{operation}.md",
        origin=RegistrationOrigin(thread_id, turn_id, "artifact_tool"),
        db=db,
    )
    if operation in {"accept", "accept_with_follow_up"}:
        node = submit_review_evidence(node.id, db=db)
    else:
        node = service.get_node(node.id, db=db)
    params = {"node_id": node.id, "expected_revision": node.revision}
    if operation == "accept":
        params["verification_result"] = "passed"
    elif operation == "accept_with_follow_up":
        params.update({
            "title": "Residual work",
            "acceptance_criteria": "Residual work is complete",
            "size": "S",
            "verification_result": "passed",
        })
    return thread_id, plan, node, artifact, params


def _artifact_frames(sent):
    return [payload for _user, payload in sent if payload["type"] == "artifacts_changed"]


async def _dispatch(surface, db, monkeypatch, thread_id, plan, operation, params):
    row = db.execute(
        "SELECT user_id,project_id FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    if surface == "http":
        from api import plans as api_plans

        monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
        monkeypatch.setattr(api_plans, "run_in_db", _immediate)
        app = FastAPI()
        app.include_router(api_plans.router)
        app.dependency_overrides[api_plans.require_user] = lambda: {"id": row["user_id"]}
        response = TestClient(app).post(
            f"/api/plans/{plan.id}/mutations",
            json={
                "operation": operation,
                "mutation_id": f"http-{operation}-{thread_id}",
                "params": params,
            },
        )
        assert response.status_code == 200, response.text
        return

    from tools import plans as tool_plans

    monkeypatch.setattr(tool_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(tool_plans, "run_in_db", _immediate)
    monkeypatch.setattr(
        tool_plans,
        "_require_context",
        lambda _kwargs: (
            thread_id,
            {"user_id": row["user_id"], "project_id": row["project_id"]},
        ),
    )
    result = await tool_plans.PlanUpdateTool().execute(
        plan_id=plan.id,
        operation=operation,
        mutation_id=f"tool-{operation}-{thread_id}",
        params=params,
    )
    assert not result.startswith("Error:"), result


@pytest.mark.usefixtures("isolated_db")
@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ("http", "tool"))
@pytest.mark.parametrize(
    "operation", ("accept", "fail", "archive_node", "accept_with_follow_up"),
)
async def test_terminal_surfaces_deliver_canonical_artifact_frame(
    surface, operation, monkeypatch,
):
    connection = get_db()
    thread_id, plan, _node, artifact, params = _node_and_artifact(
        connection, operation,
    )
    sent = []
    monkeypatch.setattr(
        notifications.state,
        "broadcast_to_user",
        lambda user_id, payload: sent.append((user_id, payload)),
    )

    await _dispatch(
        surface, connection, monkeypatch, thread_id, plan, operation, params,
    )

    assert _artifact_frames(sent) == [{
        "type": "artifacts_changed",
        "artifact_id": artifact["id"],
        "owner_thread_id": thread_id,
        "affected_thread_ids": [thread_id],
    }]


@pytest.mark.usefixtures("isolated_db")
def test_retirement_batches_multiple_rows_and_preserves_quiet_and_pinned():
    connection = get_db()
    thread_id, _plan, node, first, _params = _node_and_artifact(
        connection, "fail",
    )
    turn_id = f"turn-fail-{thread_id}"
    quiet = models.register_artifact(
        integration="workspace", type="document", external_id=f"quiet-{thread_id}.md",
        parent_thread_id=thread_id, title="quiet.md",
        origin=RegistrationOrigin(thread_id, turn_id, "artifact_tool"), db=connection,
    )
    pinned = models.register_artifact(
        integration="workspace", type="document", external_id=f"pinned-{thread_id}.md",
        parent_thread_id=thread_id, title="pinned.md",
        origin=RegistrationOrigin(thread_id, turn_id, "artifact_tool"), db=connection,
    )
    models.update_surfacing(
        quiet["id"], surfacing="quiet", parent_thread_id=thread_id, db=connection,
    )
    pinning.update_pinned(
        pinned["id"], pinned=True, parent_thread_id=thread_id, db=connection,
    )
    floor = capture_event_floor(connection)

    service.transition_node(node.id, "failed", db=connection)

    rows = {
        row["artifact_id"]: row
        for row in connection.execute(
            """SELECT * FROM artifact_registrations
               WHERE artifact_id IN (?,?,?)""",
            (first["id"], quiet["id"], pinned["id"]),
        )
    }
    assert rows[first["id"]]["surfacing"] == "settled"
    assert rows[first["id"]]["retired_at"] is not None
    assert rows[quiet["id"]]["surfacing"] == "quiet"
    assert rows[quiet["id"]]["retired_at"] is not None
    # The pinned row still gets the permanent retirement fact stamped -- pin
    # only keeps it visible, it does not erase that the task finished.
    assert rows[pinned["id"]]["surfacing"] == "visible"
    assert rows[pinned["id"]]["retired_at"] is not None
    events = connection.execute(
        """SELECT id,artifact_id FROM artifact_registration_events
           WHERE rowid>? AND event_type='origin_node_retired' ORDER BY artifact_id""",
        (floor,),
    ).fetchall()
    assert len(events) == 3
    assert len({row["id"] for row in events}) == 3
    messages = after_commit(floor, db=connection)
    assert {message["artifact_id"] for _user, message in messages} == {
        first["id"], quiet["id"], pinned["id"],
    }
    assert all(
        message["affected_thread_ids"] == [thread_id]
        for _user, message in messages
    )


def _bind_decision_artifact(connection, decision, name):
    artifact = models.register_artifact(
        integration="workspace",
        type="document",
        external_id=name,
        parent_thread_id="owner",
        title=name,
        db=connection,
    )
    connection.execute(
        """UPDATE artifact_registrations SET origin_plan_node_id=?
           WHERE artifact_id=? AND parent_thread_id='owner'""",
        (decision["node_id"], artifact["id"]),
    )
    connection.commit()
    return artifact


def test_decision_answer_http_delivers_artifact_retirement(db, monkeypatch):
    _plan, _source, decision = _raised_decision(db)
    artifact = _bind_decision_artifact(db, decision, "decision-answer.md")
    sent = []
    monkeypatch.setattr(
        notifications.state,
        "broadcast_to_user",
        lambda user_id, payload: sent.append((user_id, payload)),
    )

    response = _client(db, monkeypatch).post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={
            "option_id": "A",
            "expected_revision": decision["revision"],
            "mutation_id": "decision-answer-artifact",
        },
    )

    assert response.status_code == 200, response.text
    assert _artifact_frames(sent) == [{
        "type": "artifacts_changed",
        "artifact_id": artifact["id"],
        "owner_thread_id": "owner",
        "affected_thread_ids": ["owner"],
    }]


@pytest.mark.asyncio
async def test_decision_invalidate_tool_delivers_artifact_retirement(
    db, monkeypatch,
):
    plan, _source, decision = _raised_decision(db)
    artifact = _bind_decision_artifact(db, decision, "decision-invalidate.md")
    sent = []
    monkeypatch.setattr(
        notifications.state,
        "broadcast_to_user",
        lambda user_id, payload: sent.append((user_id, payload)),
    )
    from tools import plans as tool_plans

    monkeypatch.setattr(tool_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(tool_plans, "run_in_db", _immediate)
    monkeypatch.setattr(
        tool_plans,
        "_require_context",
        lambda _kwargs: (
            "owner", {"user_id": "user-1", "project_id": "project-1"},
        ),
    )

    result = await tool_plans.PlanDecisionTool().execute(
        plan_id=plan.id,
        operation="invalidate",
        mutation_id="decision-invalidate-artifact",
        params={
            "decision_id": decision["id"],
            "expected_revision": decision["revision"],
        },
    )

    assert not result.startswith("Error:"), result
    assert json.loads(result)["decision"]["state"] == "invalidated"
    assert _artifact_frames(sent) == [{
        "type": "artifacts_changed",
        "artifact_id": artifact["id"],
        "owner_thread_id": "owner",
        "affected_thread_ids": ["owner"],
    }]
