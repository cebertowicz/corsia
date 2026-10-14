"""The boundary rule for lane-settled review nodes.

**While a question is open, the lane that asked it owns its review node's state.
The generic plan boundary may read, position, and extend that node; it may never
drive or end it.**

A decision's review node is a real plan task — PRD-PLANS A10 says a gate assigned
to the user *is* a plan task, and it is read, ordered, indicated, and progressed
like any other node. What is not ordinary is who ends it: the terminal state is
the record of an answer the lane holds, written in the same transaction as the
answer itself.

Red at `ac1f9f8d`: the boundary treats them as ordinary work, so a generic
`activate` silently starves the queue, a generic `fail`/`accept` makes archive
and the retention sweep raise on an illegal transition, and an authenticated
human can record a passed verification for an answer nobody gave.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plans import service
from plans.boundary import PlanActor, mutate
from tests.test_attention_queue import (  # noqa: F401 - shared fixtures
    _mid,
    _seed,
    db,
    queue_for,
    raise_decision,
)
from tests.test_attention_queue_api import _DbProxy, _immediate
from tests.plan_writer_authorizer import raw_plan_seed

ROOT = PlanActor("root", "user-1", "project-1")
HUMAN = PlanActor(None, "user-1", None, kind="user")

#: Every operation `tools/plans.py` publishes that drives, ends, or restates a
#: node's own intent, with the params the boundary requires for each.
SETTLING = {
    "edit": {"title": "Renamed by an outsider"},
    "activate": {},
    "request_review": {},
    "reject": {},
    "fail": {},
    "accept": {"verification_result": "passed", "verified_by": "someone"},
    "add_correction": {
        "title": "Correct the decision-owned result",
        "acceptance_criteria": "Correction passes",
        "affected_nodes": [],
    },
    "accept_with_follow_up": {
        "title": "Follow up", "acceptance_criteria": "Follow-up DOD",
        "verification_result": "passed", "verified_by": "someone",
    },
    "add_follow_up": {
        "title": "Residual work", "acceptance_criteria": "Residual DOD",
    },
    "archive_node": {},
    # Closing ends the node outright, exactly like the archive above. Rule 1b
    # still holds: the refusal names invalidate, after which the node closes.
    "close_node": {"reason": "no longer wanted"},
    "attach_spawned_thread": {"spawned_thread_id": "worker"},
    "arm_watch": {},
    "resolve_watch": {},
}

#: Operations that make no claim about the question: they position or extend it.
STRUCTURAL = {
    "reorder": {"order_key": 0},
    # The sibling inherits the decision's user assignee, so it is a person step.
    "split": {"title": "Split part", "acceptance_criteria": "Split DOD",
              "description": "Answer the split part.\n\nAlready tried: the agent cannot decide it."},
}


def _client(db, monkeypatch, user_id: str = "user-1") -> TestClient:
    from api import plans as api_plans

    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {
        "id": user_id, "token_kind": "session",
    }
    return TestClient(app)


def _node(db, node_id: str):
    return db.execute(
        "SELECT * FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()


def _body(db, decision_id: str) -> dict:
    import json

    row = db.execute(
        """SELECT metadata FROM artifact_identities
           WHERE integration='local' AND type='plan-decision' AND external_id=?""",
        (decision_id,),
    ).fetchone()
    return json.loads(row["metadata"])["plan_decision"]


def _attention(db, decision_id: str):
    import json

    row = db.execute(
        """SELECT metadata FROM artifact_identities
           WHERE integration='local' AND type='plan-decision' AND external_id=?""",
        (decision_id,),
    ).fetchone()
    return json.loads(row["metadata"]).get("attention_state")


def _mutate(db, plan_id, operation, node_id, actor, **extra):
    node = _node(db, node_id)
    params = {"node_id": node_id, "expected_revision": node["revision"], **extra}
    return mutate(
        plan_id, operation, params, actor=actor, provenance="test",
        mutation_id=_mid(), db=db,
    )


# --- 1. the rule, across every operation and both actor kinds ---------------


@pytest.mark.parametrize(("operation", "actor_name"), [
    (operation, actor_name)
    for operation in sorted(SETTLING)
    for actor_name in ("thread", "human")
    # The user's Won't do withdraws the question instead
    # (test_decision_lifecycle: test_the_users_wont_do_on_a_question_withdraws_it).
    if (operation, actor_name) != ("close_node", "human")
])
def test_the_boundary_refuses_to_drive_an_open_decision_review_node(
    db, operation, actor_name,
):
    decision = raise_decision(db, "root", "root")
    actor = ROOT if actor_name == "thread" else HUMAN
    before_node = _node(db, decision["node_id"])["state"]
    before_revision = _node(db, decision["node_id"])["revision"]

    with pytest.raises((service.PlanError, PermissionError)) as excinfo:
        _mutate(db, decision["plan_id"], operation, decision["node_id"], actor,
                **SETTLING[operation])
    db.rollback()

    # Refused before anything moved — never an illegal-transition crash after.
    assert "illegal plan node transition" not in str(excinfo.value)
    after = _node(db, decision["node_id"])
    assert after["state"] == before_node
    assert after["revision"] == before_revision
    assert _body(db, decision["id"])["state"] == "raised"
    assert _attention(db, decision["id"]) == "raised"


def test_the_refusal_names_the_operation_that_does_own_the_decision(db):
    decision = raise_decision(db, "root", "root")

    with pytest.raises(service.PlanError) as excinfo:
        _mutate(db, decision["plan_id"], "fail", decision["node_id"], ROOT)
    db.rollback()

    message = str(excinfo.value)
    assert "decision awaiting the user" in message
    assert "invalidate" in message


def test_the_refusal_discloses_no_internal_identity(db):
    decision = raise_decision(db, "root", "root")

    with pytest.raises(service.PlanError) as excinfo:
        _mutate(db, decision["plan_id"], "fail", decision["node_id"], ROOT)
    db.rollback()

    message = str(excinfo.value)
    for secret in (decision["id"], decision["plan_id"], decision["node_id"]):
        assert secret not in message


def test_a_refused_generic_mutation_is_a_fixable_422_not_a_conflict(db, monkeypatch):
    decision = raise_decision(db, "root", "root")
    node = _node(db, decision["node_id"])
    client = _client(db, monkeypatch)

    response = client.post(
        f"/api/plans/{decision['plan_id']}/mutations",
        json={"operation": "fail", "mutation_id": _mid(),
              "params": {"node_id": node["id"],
                         "expected_revision": node["revision"]}},
    )

    assert response.status_code == 422, response.text
    assert "invalidate" in response.json()["detail"]


@pytest.mark.parametrize("operation", sorted(STRUCTURAL))
def test_structure_only_operations_remain_available(db, operation):
    """Positioning and extending make no claim about the question."""
    decision = raise_decision(db, "root", "root")

    _mutate(db, decision["plan_id"], operation, decision["node_id"], ROOT,
            **STRUCTURAL[operation])

    assert _node(db, decision["node_id"])["state"] == "review_requested"
    assert _body(db, decision["id"])["state"] == "raised"


def test_the_node_stays_readable_and_positioned_in_its_plan(db):
    from plans import rollup

    decision = raise_decision(db, "root", "root")
    db.commit()

    projected = rollup.rollup_thread(decision["plan_id"], db=db)

    assert decision["node_id"] in [node["id"] for node in projected.nodes]


def test_the_rule_lifts_once_the_question_is_settled(db, monkeypatch):
    """An answered decision's node is an ordinary terminal plan node again."""
    from plans import decisions

    decision = raise_decision(db, "root", "root")
    decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=HUMAN, provenance="p", db=db,
    )
    db.commit()

    from plans import node_settlement

    assert node_settlement.owning_lane(db, decision["node_id"]) is None


def test_an_ordinary_task_node_is_untouched_by_the_rule(db):
    from plans import node_settlement

    plan = service.ensure_plan("thread", "root", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Ordinary work", acceptance_criteria="DOD", db=db,
    )
    db.commit()

    assert node_settlement.owning_lane(db, node.id) is None
    _mutate(db, plan.id, "activate", node.id, ROOT)
    assert _node(db, node.id)["state"] == "active"


# --- 3. withdrawal is total, so archive and the sweep never fail -------------


def _archive(db, thread_id: str) -> None:
    from core import thread_lifecycle
    from infra.database import write_transaction

    db.commit()
    with write_transaction(db, scope="archive"):
        thread_lifecycle.set_user_archive_state(db, thread_id, archived=True)


def _force_node_state(db, node_id: str, state: str) -> None:
    """Put the node in a state a pre-rule row could already be in."""
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state=? WHERE id=?", (state, node_id))
    db.commit()


@pytest.mark.parametrize(
    "terminal", ["failed", "accepted", "accepted_with_followup"],
)
def test_archiving_the_owner_thread_survives_an_already_terminal_review_node(
    db, terminal,
):
    decision = raise_decision(db, "root", "root")
    _force_node_state(db, decision["node_id"], terminal)

    _archive(db, "root")

    assert _body(db, decision["id"])["state"] == "invalidated"
    # The node keeps the terminal state it already had; nothing illegal is tried.
    assert _node(db, decision["node_id"])["state"] == terminal
    assert _attention(db, decision["id"]) != "raised"


def test_the_retention_sweep_completes_with_such_a_row(db, tmp_path):
    from core import thread_lifecycle

    decision = raise_decision(db, "root", "root")
    _force_node_state(db, decision["node_id"], "failed")
    db.execute(
        "UPDATE threads SET lifecycle_state='archived', archived_at='2020-01-01T00:00:00+00:00' "
        "WHERE id='root'",
    )
    db.commit()

    report = thread_lifecycle.run_retention_sweep(
        db, config=thread_lifecycle.LifecycleConfig(), workspace=tmp_path,
    )

    assert report is not None


def test_invalidate_settles_a_decision_whose_node_is_already_terminal(db):
    from plans import decisions

    decision = raise_decision(db, "root", "root")
    _force_node_state(db, decision["node_id"], "failed")

    decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=ROOT, provenance="p", db=db,
    )
    db.commit()

    assert _body(db, decision["id"])["state"] == "invalidated"
    assert _attention(db, decision["id"]) != "raised"


def test_withdrawal_stays_idempotent_across_repeated_archival(db):
    from core import thread_lifecycle
    from infra.database import write_transaction

    decision = raise_decision(db, "root", "root")
    _archive(db, "root")
    first = _body(db, decision["id"])

    db.commit()
    with write_transaction(db, scope="unarchive"):
        thread_lifecycle.set_user_archive_state(db, "root", archived=False)
    _archive(db, "root")

    assert _body(db, decision["id"]) == first


# --- 4. the read side never offers a row the answer route cannot settle ------


@pytest.mark.parametrize(
    "state", ["pending", "active", "failed", "accepted"],
)
def test_a_review_node_outside_review_requested_is_never_the_queue_head(db, state):
    decision = raise_decision(db, "root", "root")
    _force_node_state(db, decision["node_id"], state)

    assert queue_for(db) == []


def test_such_a_row_stays_inspectable_rather_than_disappearing(db, monkeypatch):
    from api import attention as api_attention

    decision = raise_decision(db, "root", "root")
    _force_node_state(db, decision["node_id"], "active")
    monkeypatch.setattr(api_attention, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_attention, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_attention.router)
    app.dependency_overrides[api_attention.require_user] = lambda: {"id": "user-1"}
    client = TestClient(app)

    raised = client.get("/api/attention/raised")

    assert raised.status_code == 200
    assert decision["id"] in [item["external_id"] for item in raised.json()["items"]]


def test_a_desynced_row_never_starves_a_later_live_decision(db):
    stranded = raise_decision(db, "root", "root", title="STRANDED")
    _force_node_state(db, stranded["node_id"], "active")
    raise_decision(db, "other-root", "other-root", title="LIVE")
    db.commit()

    queued = queue_for(db)

    assert [row["detail"]["title"] for row in queued] == ["LIVE"]


def test_the_plan_indicator_and_the_user_queue_still_agree(db):
    from plans import decision_projection

    decision = raise_decision(db, "root", "root")
    _force_node_state(db, decision["node_id"], "active")

    assert queue_for(db) == []
    assert decision_projection.awaiting_count([decision["plan_id"]], db=db) == 0


def test_a_live_decision_is_still_offered_and_answerable(db):
    from plans import decisions

    decision = raise_decision(db, "root", "root")

    assert len(queue_for(db)) == 1
    decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=HUMAN, provenance="p", db=db,
    )
    db.commit()

    assert queue_for(db) == []
    assert _body(db, decision["id"])["state"] == "answered"


# --- 5. the outcome the rule exists to prevent -------------------------------


def test_no_user_review_node_is_accepted_without_a_recorded_answer(db):
    """The authenticated human could record a passed verification for nothing."""
    decision = raise_decision(db, "root", "root")

    with pytest.raises((service.PlanError, PermissionError)):
        _mutate(db, decision["plan_id"], "accept", decision["node_id"], HUMAN,
                verification_result="passed", verified_by="user:user-1")
    db.rollback()

    node = _node(db, decision["node_id"])
    assert node["state"] == "review_requested"
    accepted = db.execute(
        """SELECT COUNT(*) AS n FROM plan_node_events
           WHERE node_id=? AND new_state='accepted'""",
        (decision["node_id"],),
    ).fetchone()["n"]
    assert accepted == 0
    assert _body(db, decision["id"])["answer"] is None


def test_a_question_leaving_the_queue_always_leaves_an_audit_trail(db):
    from plans import decisions

    decision = raise_decision(db, "root", "root")
    decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=ROOT, provenance="p", db=db,
    )
    db.commit()

    events = [
        row["event"] for row in db.execute(
            "SELECT event FROM plan_node_events WHERE node_id=?",
            (decision["node_id"],),
        ).fetchall()
    ]
    assert "decision_invalidated" in events


@pytest.mark.parametrize("operation", sorted(SETTLING))
def test_invalidate_remains_the_escape_hatch_after_any_refusal(db, operation):
    from plans import decisions

    decision = raise_decision(db, "root", "root")
    with pytest.raises((service.PlanError, PermissionError)):
        _mutate(db, decision["plan_id"], operation, decision["node_id"], ROOT,
                **SETTLING[operation])
    db.rollback()

    decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=ROOT, provenance="p", db=db,
    )
    db.commit()

    assert _body(db, decision["id"])["state"] == "invalidated"


# --- 6. races, sequential but real -------------------------------------------


def test_answer_then_archive_is_coherent(db):
    from plans import decisions

    decision = raise_decision(db, "root", "root")
    decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=HUMAN, provenance="p", db=db,
    )
    db.commit()

    _archive(db, "root")

    assert _body(db, decision["id"])["state"] == "answered"
    assert _node(db, decision["node_id"])["state"] == "accepted"


def test_archive_then_answer_refuses_truthfully(db, monkeypatch):
    decision = raise_decision(db, "root", "root")
    client = _client(db, monkeypatch)

    _archive(db, "root")
    response = client.post(
        f"/api/plans/decisions/{decision['id']}/answer",
        json={"option_id": "A", "expected_revision": decision["revision"],
              "mutation_id": _mid()},
    )

    assert response.status_code in (403, 404, 409, 422)
    assert queue_for(db) == []
    assert _body(db, decision["id"])["state"] == "invalidated"
