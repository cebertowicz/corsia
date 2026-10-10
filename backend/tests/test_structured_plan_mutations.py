"""Focused P2a contract tests for plan mutation and actor authorization."""

from __future__ import annotations

from collections.abc import Iterator

import importlib.util
import itertools
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from infra.schema import SCHEMA_SQL
from plans import mutations, service
from plans.boundary import PlanActor, mutate


class _DbProxy:
    """Keep a fixture-owned connection open when API/tool code closes its handle."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        return None


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for thread_id, user_id, spawned_by in (
        ("parent", "user-1", None),
        ("worker", "user-1", "parent"),
        ("sibling", "user-1", "parent"),
        ("other", "user-2", None),
    ):
        connection.execute(
            "INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) VALUES (?,?,?,?,?)",
            (thread_id, user_id, spawned_by, "t", "t"),
        )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def plan_and_node(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Implement", acceptance_criteria="Tests pass", db=db,
    )
    return plan, node


def _actor(thread_id="parent", user_id="user-1"):
    return PlanActor(thread_id=thread_id, user_id=user_id, project_id=None)


def _human_actor():
    return PlanActor(thread_id=None, user_id="user-1", project_id=None, kind="user")


_MUTATION_IDS = itertools.count(1)


def _mutate(db, plan_id, operation, params, actor=None, mutation_id=None):
    deferred = mutate(
        plan_id, operation, params, actor=actor or _actor(), provenance="test",
        mutation_id=mutation_id or f"test-{next(_MUTATION_IDS)}", db=db,
    )
    return deferred.result


def test_insert_append_split_edit_and_attach_keep_identity_and_events(db, plan_and_node):
    plan, node = plan_and_node

    appended = _mutate(
        db, plan.id, "insert",
        {"kind": "task", "title": "Append", "acceptance_criteria": "Append passes"},
    )["node"]
    split = _mutate(
        db, plan.id, "split",
        {"node_id": node.id, "title": "Remainder", "acceptance_criteria": "Remainder passes",
         "expected_revision": node.revision},
    )["node"]
    source = service.get_node(node.id, db=db)
    restated = _mutate(
        db, plan.id, "edit",
        {"node_id": node.id, "title": "Implement safely", "expected_revision": source.revision},
    )["node"]
    attached = _mutate(
        db, plan.id, "attach_spawned_thread",
        {"node_id": node.id, "spawned_thread_id": "worker", "expected_revision": restated["revision"]},
    )["node"]

    assert appended["order_key"] == 1
    assert split["order_key"] == 1
    assert restated["id"] == node.id
    assert attached["spawned_thread_id"] == "worker"
    events = [row["event"] for row in db.execute(
        "SELECT event FROM plan_node_events WHERE node_id=? ORDER BY created_at", (node.id,)
    )]
    assert {"node_restated", "node_split", "spawned_thread_attached"} <= set(events)


def test_creation_rejects_extra_operation_fields(db, plan_and_node):
    plan, _node = plan_and_node

    with pytest.raises(service.PlanError, match="unexpected parameters"):
        _mutate(
            db, plan.id, "add",
            {"kind": "task", "title": "Extra", "acceptance_criteria": "Never",
             "before_node_id": "anything"},
        )


def test_reorder_and_activate_reject_stale_revisions(db, plan_and_node):
    plan, node = plan_and_node
    service.add_node(plan.id, kind="task", title="Second", acceptance_criteria="Second passes", db=db)
    _mutate(
        db, plan.id, "edit",
        {"node_id": node.id, "title": "Changed", "expected_revision": node.revision},
    )

    with pytest.raises(service.PlanError, match="revision conflict"):
        _mutate(
            db, plan.id, "reorder",
            {"node_id": node.id, "order_key": 1, "expected_revision": node.revision},
        )
    with pytest.raises(service.PlanError, match="revision conflict"):
        _mutate(
            db, plan.id, "activate",
            {"node_id": node.id, "expected_revision": node.revision},
        )


def test_thread_actor_cannot_mutate_project_or_workspace_scope_plans(db, plan_and_node):
    from infra import config

    _plan, _node = plan_and_node
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-1','Project','t','t')"
    )
    db.execute("UPDATE threads SET project_id='project-1' WHERE id='parent'")
    db.commit()
    actor = PlanActor("parent", "user-1", "project-1")
    project_plan = service.ensure_plan("project_scope", "project-1", db=db)
    workspace_plan = service.ensure_plan(
        "workspace_scope", config.WORKSPACE_SELECTION.workspace_id, db=db,
    )
    params = {"kind": "task", "title": "Blocked", "acceptance_criteria": "Never"}

    with pytest.raises(PermissionError, match="project scope"):
        _mutate(db, project_plan.id, "add", params, actor=actor)
    with pytest.raises(PermissionError, match="workspace scope"):
        _mutate(db, workspace_plan.id, "add", params, actor=actor)


def test_insert_cannot_target_a_node_in_another_plan(db, plan_and_node):
    plan, _node = plan_and_node
    other_plan = service.ensure_plan("thread", "other", db=db)
    other = service.add_node(
        other_plan.id, kind="task", title="Other", acceptance_criteria="Other passes", db=db,
    )

    with pytest.raises(PermissionError, match="insert target"):
        _mutate(
            db, plan.id, "insert",
            {"kind": "task", "title": "Bad", "acceptance_criteria": "Bad DOD",
             "before_node_id": other.id},
        )


def test_plan_owner_rejects_with_feedback_and_a_non_owner_thread_is_denied(db, plan_and_node):
    """Only the plan owner (or a human) reviews; another thread of its user cannot."""
    plan, node = plan_and_node
    _mutate(db, plan.id, "activate", {"node_id": node.id, "expected_revision": node.revision})
    reviewed = _mutate(
        db, plan.id, "request_review",
        {
            "node_id": node.id,
            "evidence": "Focused proof passed",
            "expected_revision": node.revision + 1,
        },
    )["node"]

    with pytest.raises(PermissionError, match="requires the plan owner or human fallback"):
        _mutate(
            db, plan.id, "reject",
            {"node_id": node.id, "feedback": "Fix tests", "expected_revision": reviewed["revision"]},
            actor=_actor("sibling"),
        )

    rejection = {"node_id": node.id, "feedback": "Fix tests", "expected_revision": reviewed["revision"]}
    outcome = _mutate(db, plan.id, "reject", rejection, mutation_id="review-reject")
    replay = _mutate(db, plan.id, "reject", rejection, mutation_id="review-reject")
    rejected = outcome["node"]

    # The submission is settled; its one linked revision is returned by identity.
    assert rejected["state"] == "closed"
    assert outcome["revision"]["source_node_id"] == node.id
    assert outcome["revision"]["node_id"] != node.id
    assert replay == outcome
    feedback_events = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='review_rejected'",
        (node.id,),
    ).fetchall()
    assert len(feedback_events) == 1
    feedback = feedback_events[0][0]
    assert json.loads(feedback) == {"feedback": "Fix tests"}
    assert db.execute(
        "SELECT COUNT(*) FROM plan_mutation_receipts WHERE plan_id=? AND mutation_id=?",
        (plan.id, "review-reject"),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("initial_state", ("pending", "active"))
def test_reject_requires_review_requested_without_side_effects(db, plan_and_node, initial_state):
    """Reject only a review-requested node; receipts own a same-key retry after success."""
    plan, node = plan_and_node
    node_id, expected_state, expected_revision = node.id, node.state, node.revision
    if initial_state == "active":
        activated = _mutate(
            db, plan.id, "activate", {"node_id": node_id, "expected_revision": expected_revision},
        )["node"]
        expected_state, expected_revision = activated["state"], activated["revision"]
    params = {
        "node_id": node_id,
        "feedback": "Fix tests",
        "expected_revision": expected_revision,
    }
    mutation_id = f"reject-{initial_state}"

    with pytest.raises(service.PlanError, match="review_requested"):
        _mutate(db, plan.id, "reject", params, mutation_id=mutation_id)

    unchanged = service.get_node(node_id, db=db)
    assert (unchanged.state, unchanged.revision) == (expected_state, expected_revision)
    assert db.execute(
        "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND event='review_rejected'",
        (node_id,),
    ).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM plan_mutation_receipts WHERE plan_id=? AND mutation_id=?",
        (plan.id, mutation_id),
    ).fetchone()[0] == 0


def test_human_reviews_a_user_assigned_task_and_records_user_provenance(db, plan_and_node):
    plan, _node = plan_and_node
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-1','Project','t','t')"
    )
    db.execute("UPDATE threads SET project_id='project-1' WHERE id='parent'")
    db.commit()
    user_task = service.add_node(
        plan.id, kind="task", title="Human review", acceptance_criteria="Review passes",
        assignee="user", db=db,
    )
    # The task is the user's to complete, so the user drives it; an agent's
    # activate would leave it untouched.
    _mutate(
        db, plan.id, "activate",
        {"node_id": user_task.id, "expected_revision": user_task.revision},
        actor=_human_actor(),
    )
    waiting = _mutate(
        db, plan.id, "request_review",
        {
            "node_id": user_task.id,
            "evidence": "Focused proof passed",
            "expected_revision": user_task.revision + 1,
        },
        actor=_human_actor(),
    )["node"]
    accepted = _mutate(
        db, plan.id, "accept",
        {"node_id": user_task.id, "verification_result": "passed",
         "expected_revision": waiting["revision"]},
        actor=_human_actor(),
    )["node"]

    assert accepted["state"] == "accepted"
    actor_id = db.execute(
        "SELECT actor_id FROM plan_node_events WHERE node_id=? AND new_state='accepted'",
        (user_task.id,),
    ).fetchone()[0]
    assert actor_id == "user:user-1"


def test_root_review_allows_owner_settlement_and_authenticated_human_fallback(db):
    """An undelegated root has a truthful terminal route without a parent tier."""
    plan = service.ensure_plan("thread", "parent", db=db)
    owner_task = service.add_node(
        plan.id, kind="task", title="Owner review", acceptance_criteria="Review passes", db=db,
    )
    active = _mutate(
        db, plan.id, "activate",
        {"node_id": owner_task.id, "expected_revision": owner_task.revision},
    )["node"]
    waiting = _mutate(
        db, plan.id, "request_review",
        {
            "node_id": owner_task.id,
            "evidence": "Focused proof passed",
            "expected_revision": active["revision"],
        },
    )["node"]

    owner_accepted = _mutate(
        db,
        plan.id,
        "accept",
        {
            "node_id": owner_task.id,
            "verification_result": "passed",
            "expected_revision": waiting["revision"],
        },
        actor=_actor("parent"),
    )["node"]
    assert service.progress(plan.id, db=db).liveness == "idle"

    follow_source = service.add_node(
        plan.id, kind="task", title="Owner follow-up review",
        acceptance_criteria="Review passes", db=db,
    )
    active = _mutate(
        db, plan.id, "activate",
        {"node_id": follow_source.id, "expected_revision": follow_source.revision},
    )["node"]
    waiting = _mutate(
        db, plan.id, "request_review",
        {
            "node_id": follow_source.id,
            "evidence": "Focused proof passed",
            "expected_revision": active["revision"],
        },
    )["node"]
    owner_with_follow_up = _mutate(
        db,
        plan.id,
        "accept_with_follow_up",
        {
            "node_id": follow_source.id,
            "title": "Bounded residual",
            "acceptance_criteria": "Residual is verified",
            "verification_result": "passed",
            "expected_revision": waiting["revision"],
        },
        actor=_actor("parent"),
    )

    human_task = service.add_node(
        plan.id, kind="task", title="Human fallback",
        acceptance_criteria="Review passes", db=db,
    )
    active = _mutate(
        db, plan.id, "activate",
        {"node_id": human_task.id, "expected_revision": human_task.revision},
    )["node"]
    waiting = _mutate(
        db, plan.id, "request_review",
        {
            "node_id": human_task.id,
            "evidence": "Focused proof passed",
            "expected_revision": active["revision"],
        },
    )["node"]
    human_accepted = _mutate(
        db,
        plan.id,
        "accept",
        {
            "node_id": human_task.id,
            "verification_result": "passed",
            "expected_revision": waiting["revision"],
        },
        actor=_human_actor(),
    )["node"]

    assert owner_accepted["state"] == "accepted"
    assert owner_with_follow_up["node"]["state"] == "accepted_with_followup"
    assert owner_with_follow_up["follow_up"]["follow_up_of"] == follow_source.id
    assert human_accepted["state"] == "accepted"


def test_acceptance_binds_reviewer_identity_and_rejects_stale_revision(db, plan_and_node):
    plan, node = plan_and_node
    _mutate(db, plan.id, "activate", {"node_id": node.id, "expected_revision": node.revision})
    reviewed = _mutate(
        db, plan.id, "request_review",
        {
            "node_id": node.id,
            "evidence": "Focused proof passed",
            "expected_revision": node.revision + 1,
        },
    )["node"]
    _mutate(
        db, plan.id, "edit",
        {"node_id": node.id, "description": "Fresh evidence", "expected_revision": reviewed["revision"]},
    )

    with pytest.raises(service.PlanError, match="revision conflict"):
        _mutate(
            db, plan.id, "accept",
            {"node_id": node.id, "verification_result": "passed",
             "expected_revision": reviewed["revision"]},
        )
    current = service.get_node(node.id, db=db)
    with pytest.raises(service.PlanError, match="unexpected parameters"):
        _mutate(
            db, plan.id, "accept",
            {"node_id": node.id, "verification_result": "passed",
             "expected_revision": current.revision, "verified_by": "forged"},
        )

    accepted = _mutate(
        db, plan.id, "accept",
        {"node_id": node.id, "verification_result": "passed",
         "expected_revision": current.revision},
    )["node"]
    assert accepted["state"] == "accepted"
    event = db.execute(
        "SELECT actor_id, payload_json FROM plan_node_events WHERE node_id=? AND new_state='accepted'",
        (node.id,),
    ).fetchone()
    assert event["actor_id"] == "parent"
    assert json.loads(event["payload_json"]) == {"verified_by": "parent"}


@pytest.mark.asyncio
async def test_http_route_derives_human_actor_and_rejects_selected_thread(db, plan_and_node, monkeypatch):
    route_path = Path(__file__).parents[1] / "api" / "plans.py"
    spec = importlib.util.spec_from_file_location("p2a_plans_api", route_path)
    assert spec and spec.loader
    api_plans = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api_plans)

    plan, _node = plan_and_node
    review_task = service.add_node(
        plan.id, kind="task", title="Human review", acceptance_criteria="Review passes",
        assignee="user", db=db,
    )
    service.transition_node(review_task.id, "active", db=db)
    from plans import review_evidence

    waiting = review_evidence.submit(
        review_task.id,
        evidence="Focused proof passed",
        expected_revision=service.get_node(review_task.id, db=db).revision,
        actor_id="parent",
        provenance="test.structured_plan_mutations",
        db=db,
    )
    proxy = _DbProxy(db)

    async def immediate(callable_):
        return callable_()

    monkeypatch.setattr(api_plans, "get_db", lambda: proxy)
    monkeypatch.setattr(api_plans, "run_in_db", immediate)
    with pytest.raises(HTTPException, match="authenticated human"):
        await api_plans.mutate_plan(
            plan.id,
            {"operation": "accept", "mutation_id": "http-reject", "actor_session_id": "parent", "params": {}},
            {"id": "user-1"},
        )

    result = await api_plans.mutate_plan(
        plan.id,
        {"operation": "accept", "mutation_id": "http-accept", "params": {
            "node_id": review_task.id, "verification_result": "passed",
            "expected_revision": waiting.revision,
        }},
        {"id": "user-1"},
    )
    assert result["node"]["state"] == "accepted"
    actor_id = db.execute(
        "SELECT actor_id FROM plan_node_events WHERE node_id=? AND new_state='accepted'",
        (review_task.id,),
    ).fetchone()[0]
    assert actor_id == "user:user-1"


@pytest.mark.asyncio
async def test_tool_reuses_actor_bound_mutation_boundary(db, plan_and_node, monkeypatch):
    from tools import plans as tool_plans

    plan, node = plan_and_node
    proxy = _DbProxy(db)

    async def immediate(callable_):
        return callable_()

    monkeypatch.setattr(tool_plans, "_require_context", lambda _kwargs: ("parent", {
        "user_id": "user-1", "project_id": None,
    }))
    monkeypatch.setattr(tool_plans, "get_db", lambda: proxy)
    monkeypatch.setattr(tool_plans, "run_in_db", immediate)
    active = service.transition_node(node.id, "active", db=db)
    tool_result = await tool_plans.PlanUpdateTool().execute(
        plan_id=plan.id,
        mutation_id="tool-review",
        operation="request_review",
        params={
            "node_id": node.id,
            "evidence": "Focused proof passed",
            "expected_revision": active.revision,
        },
    )
    assert json.loads(tool_result)["node"]["state"] == "review_requested"


# --- reorder addresses live position, not a persisted key -------------------
#
# All three ordered reads (`current`, `projections`, `rollup`) filter
# `archived_at IS NULL`, so the only order a caller can observe is the live one
# and a requested position is a position in *that* list. At `c314478e` the
# bounds check counted live siblings while the shifts did key arithmetic over a
# domain archive had made sparse, so a move across an archived sibling landed
# one slot short. Adding an `archived_at` filter to the shifts does not fix it —
# the defect is the position/key conflation itself.


def _ordered(db, plan_id):
    return [
        row["title"] for row in db.execute(
            "SELECT title FROM plan_nodes WHERE plan_id=? AND archived_at IS NULL "
            "ORDER BY order_key, id", (plan_id,),
        ).fetchall()
    ]


def _keys(db, plan_id):
    return [
        row["order_key"] for row in db.execute(
            "SELECT order_key FROM plan_nodes WHERE plan_id=? AND archived_at IS NULL "
            "ORDER BY order_key, id", (plan_id,),
        ).fetchall()
    ]


def _four_siblings(db, archived_title: str):
    """A, B, C, D in order, with one archived so the live keys go sparse."""
    plan = service.ensure_plan("thread", "parent", db=db)
    nodes = {
        title: service.add_node(
            plan.id, kind="task", title=title, acceptance_criteria=f"{title} DOD",
            db=db,
        )
        for title in ("A", "B", "C", "D")
    }
    db.execute(
        "UPDATE plan_nodes SET archived_at=? WHERE id=?",
        (service._now(), nodes[archived_title].id),
    )
    db.commit()
    return plan, nodes


def test_reorder_lands_at_the_requested_live_position_across_an_archived_sibling(db):
    """R15. Live [A, C, D]; A to position 2 must land last, not second."""
    plan, nodes = _four_siblings(db, "B")
    assert _ordered(db, plan.id) == ["A", "C", "D"]

    mutations.reorder_node(nodes["A"].id, 2, db=db)
    db.commit()

    assert _ordered(db, plan.id) == ["C", "D", "A"]


def test_reorder_leaves_live_sibling_keys_dense_and_deterministic(db):
    """R16. Archive made the live keys sparse; a reorder must re-densify them."""
    plan, nodes = _four_siblings(db, "A")
    assert _keys(db, plan.id) == [1, 2, 3]

    mutations.reorder_node(nodes["D"].id, 0, db=db)
    db.commit()

    assert _ordered(db, plan.id) == ["D", "B", "C"]
    assert _keys(db, plan.id) == [0, 1, 2]


def test_reorder_never_shifts_an_archived_sibling(db):
    plan, nodes = _four_siblings(db, "B")
    before = db.execute(
        "SELECT order_key, revision FROM plan_nodes WHERE id=?", (nodes["B"].id,),
    ).fetchone()

    mutations.reorder_node(nodes["A"].id, 2, db=db)
    db.commit()

    after = db.execute(
        "SELECT order_key, revision FROM plan_nodes WHERE id=?", (nodes["B"].id,),
    ).fetchone()
    assert (after["order_key"], after["revision"]) == (
        before["order_key"], before["revision"],
    )


def test_reorder_to_the_same_live_position_is_idempotent(db):
    plan, nodes = _four_siblings(db, "A")

    mutations.reorder_node(nodes["D"].id, 0, db=db)
    db.commit()
    settled = service.get_node(nodes["D"].id, db=db).revision
    mutations.reorder_node(nodes["D"].id, 0, db=db)
    db.commit()

    assert service.get_node(nodes["D"].id, db=db).revision == settled
    assert _keys(db, plan.id) == [0, 1, 2]


def test_reorder_still_refuses_a_position_outside_the_live_range(db):
    plan, nodes = _four_siblings(db, "B")

    with pytest.raises(service.PlanError, match="outside the sibling range"):
        mutations.reorder_node(nodes["A"].id, 3, db=db)


def test_archiving_a_sibling_after_a_reorder_keeps_the_live_order(db):
    plan, nodes = _four_siblings(db, "B")
    mutations.reorder_node(nodes["A"].id, 2, db=db)
    db.commit()

    db.execute(
        "UPDATE plan_nodes SET archived_at=? WHERE id=?",
        (service._now(), nodes["C"].id),
    )
    db.commit()

    assert _ordered(db, plan.id) == ["D", "A"]


def test_reorder_still_rejects_a_stale_revision_across_archived_siblings(db):
    plan, nodes = _four_siblings(db, "B")
    stale = service.get_node(nodes["A"].id, db=db).revision
    mutations.edit_node(nodes["A"].id, title="A prime", db=db)
    db.commit()

    with pytest.raises(service.PlanError, match="revision conflict"):
        mutations.reorder_node(nodes["A"].id, 2, expected_revision=stale, db=db)


# --- 409 means re-read; 422 means fix it in place ---------------------------
#
# Collapsing the two makes every client message a guess. A malformed request is
# permanently malformed: telling that caller to "re-read and retry with fresh
# evidence" sends a conforming client into an infinite retry over a body it
# could have fixed. Only a target that actually moved is a 409.


async def _mutate_over_http(db, monkeypatch, plan_id, body):
    from api import plans as api_plans

    proxy = _DbProxy(db)

    async def immediate(callable_):
        return callable_()

    monkeypatch.setattr(api_plans, "get_db", lambda: proxy)
    monkeypatch.setattr(api_plans, "run_in_db", immediate)
    return await api_plans.mutate_plan(plan_id, body, {"id": "user-1"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params,expected",
    [
        ({}, "expected_revision is required"),
        ({"expected_revision": "seven"}, "non-negative integer"),
        ({"expected_revision": 1, "order_key": 0}, "unexpected parameters"),
    ],
)
async def test_a_malformed_mutation_body_is_a_422_not_a_409(
    db, plan_and_node, monkeypatch, params, expected,
):
    plan, node = plan_and_node

    with pytest.raises(HTTPException) as refusal:
        await _mutate_over_http(
            db, monkeypatch, plan.id,
            {
                "operation": "activate",
                "mutation_id": "http-shape",
                "params": {"node_id": node.id, **params},
            },
        )

    assert refusal.value.status_code == 422
    assert expected in str(refusal.value.detail)


@pytest.mark.asyncio
async def test_an_unsupported_operation_is_a_422_not_a_409(
    db, plan_and_node, monkeypatch,
):
    plan, node = plan_and_node

    with pytest.raises(HTTPException) as refusal:
        await _mutate_over_http(
            db, monkeypatch, plan.id,
            {
                "operation": "teleport",
                "mutation_id": "http-unsupported",
                "params": {"node_id": node.id, "expected_revision": 1},
            },
        )

    assert refusal.value.status_code == 422


@pytest.mark.asyncio
async def test_a_stale_revision_is_still_a_409(db, plan_and_node, monkeypatch):
    """The target moved, so re-reading genuinely helps. This is the case the
    409 advice was written for and it must not be swept into 422."""
    plan, node = plan_and_node
    service.transition_node(node.id, "active", db=db)

    with pytest.raises(HTTPException) as refusal:
        await _mutate_over_http(
            db, monkeypatch, plan.id,
            {
                "operation": "request_review",
                "mutation_id": "http-stale",
                "params": {
                    "node_id": node.id,
                    "evidence": "Focused proof passed",
                    "expected_revision": node.revision,
                },
            },
        )

    assert refusal.value.status_code == 409
    assert "revision conflict" in str(refusal.value.detail)


@pytest.mark.asyncio
async def test_an_illegal_transition_is_still_a_409(db, plan_and_node, monkeypatch):
    """State movement the caller can only resolve by re-reading."""
    plan, node = plan_and_node
    current = service.get_node(node.id, db=db)

    with pytest.raises(HTTPException) as refusal:
        await _mutate_over_http(
            db, monkeypatch, plan.id,
            {
                "operation": "request_review",
                "mutation_id": "http-illegal",
                "params": {
                    "node_id": node.id,
                    "evidence": "Focused proof passed",
                    "expected_revision": current.revision,
                },
            },
        )

    assert refusal.value.status_code == 409
    assert "requires active or review-requested work" in str(refusal.value.detail)
