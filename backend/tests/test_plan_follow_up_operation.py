"""Standalone `add_follow_up` through the generic mutation boundary (C2).

`plans.followups.add_follow_up` is already the canonical owner of a
follow-up chain, but the only production caller is `accept_with_follow_up` —
so PRD B6's *non-blocking sidetrack* (record residual work without claiming the
source is done) is unreachable. That change is exposure only: the chain owner
is reused, and no acceptance logic is duplicated.

Red at `15a45c9c`: the operation does not exist at any layer, so every test
here fails on `unsupported plan operation: add_follow_up`.
"""

from __future__ import annotations

from collections.abc import Iterator

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.schema import SCHEMA_SQL
from plans import node_waits, service
from plans.boundary import PlanActor, mutate
from tests.test_attention_queue import _mid, _seed, raise_decision
from tests.test_attention_queue_api import _DbProxy, _immediate

OWNER = PlanActor("root", "user-1", "project-1")
FOREIGN = PlanActor("foreign", "user-2", "project-2")


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    _seed(connection)
    connection.commit()
    yield connection
    connection.close()


def _plan(db, owner: str = "root"):
    plan = service.ensure_plan("thread", owner, db=db)
    db.commit()
    return plan


def _task(db, plan_id: str, title: str = "Source", **kwargs):
    node = service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} DOD", db=db, **kwargs,
    )
    db.commit()
    return node


def _row(db, node_id: str):
    return db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()


def _events(db, node_id: str, event: str) -> int:
    return db.execute(
        "SELECT COUNT(*) AS n FROM plan_node_events WHERE node_id=? AND event=?",
        (node_id, event),
    ).fetchone()["n"]


def _follow_up(db, plan_id, node_id, *, actor=OWNER, title="Residual work",
               acceptance_criteria="Residual DOD", revision=None, mutation_id=None):
    return mutate(
        plan_id, "add_follow_up",
        {
            "node_id": node_id,
            "title": title,
            "acceptance_criteria": acceptance_criteria,
            "expected_revision": (
                revision if revision is not None else _row(db, node_id)["revision"]
            ),
        },
        actor=actor, provenance="test", mutation_id=mutation_id or _mid(), db=db,
    )


# --- 1. the sidetrack is reachable, and it is a sidetrack -------------------


def test_a_standalone_follow_up_is_a_dod_bearing_task_linked_to_its_source(db):
    plan = _plan(db)
    source = _task(db, plan.id)

    result = _follow_up(db, plan.id, source.id).result

    created = _row(db, result["follow_up"]["id"])
    assert created["follow_up_of"] == source.id
    assert created["kind"] == "task"
    assert created["acceptance_criteria"] == "Residual DOD"
    assert created["state"] == "pending"


def test_a_standalone_follow_up_does_not_settle_its_source(db):
    """The whole point of B6: residual work without claiming the source is done."""
    plan = _plan(db)
    source = _task(db, plan.id)
    service.transition_node(source.id, "active", db=db)
    db.commit()

    _follow_up(db, plan.id, source.id)

    assert _row(db, source.id)["state"] == "active"
    assert _events(db, source.id, "state_transition") == 1


def test_a_standalone_follow_up_creates_no_dependency_edge(db):
    plan = _plan(db)
    source = _task(db, plan.id)

    result = _follow_up(db, plan.id, source.id).result

    edges = db.execute(
        "SELECT COUNT(*) AS n FROM plan_node_dependencies WHERE node_id=? OR "
        "depends_on_node_id=?",
        (result["follow_up"]["id"], result["follow_up"]["id"]),
    ).fetchone()["n"]
    assert edges == 0
    assert node_waits.waits_on_dependency(db, result["follow_up"]["id"], "pending") is False


def test_a_standalone_follow_up_raises_no_user_attention(db):
    plan = _plan(db)
    source = _task(db, plan.id)
    before = db.execute(
        "SELECT COUNT(*) AS n FROM plan_node_artifact_links",
    ).fetchone()["n"]

    _follow_up(db, plan.id, source.id)

    after = db.execute(
        "SELECT COUNT(*) AS n FROM plan_node_artifact_links",
    ).fetchone()["n"]
    assert after == before


# --- 2. defaults, lineage and order ----------------------------------------


def test_a_standalone_follow_up_inherits_its_source_placement_but_no_other_thread(db):
    """Placement and budget carry over. A thread an older source row still names
    does not: only the plan owner runs the follow-up."""
    plan = _plan(db)
    track = service.add_node(
        plan.id, kind="track", title="Track", acceptance_criteria="Track DOD", db=db,
    )
    db.commit()
    source = _task(
        db, plan.id, parent_id=track.id, assignee="user", watchdog_budget_seconds=900,
    )
    db.execute(
        "UPDATE plan_nodes SET owner_thread_id='child',executor_thread_id='child' WHERE id=?",
        (source.id,),
    )
    db.commit()

    created = _row(db, _follow_up(db, plan.id, source.id).result["follow_up"]["id"])

    assert created["parent_id"] == track.id
    assert created["assignee"] == "user"
    assert created["watchdog_budget_seconds"] == 900
    assert (created["owner_thread_id"], created["executor_thread_id"]) == (None, None)


def test_a_standalone_follow_up_appends_after_its_siblings(db):
    plan = _plan(db)
    source = _task(db, plan.id, "First")
    later = _task(db, plan.id, "Second")

    created = _row(db, _follow_up(db, plan.id, source.id).result["follow_up"]["id"])

    assert created["order_key"] > _row(db, later.id)["order_key"]


def test_a_follow_up_chain_of_five_keeps_every_link(db):
    """C2: a chain has no depth limit, and every follow-up links to its source."""
    plan = _plan(db)
    root = _task(db, plan.id).id
    current = root
    for _ in range(5):
        created = _follow_up(db, plan.id, current).result["follow_up"]["id"]
        assert _row(db, created)["follow_up_of"] == current
        current = created


def test_a_send_back_is_accepted_at_every_round(db):
    """A send-back meets no depth refusal at any round."""
    from plans import rejection
    from tests.plan_review_helpers import submit_review_evidence

    plan = _plan(db)
    current = _task(db, plan.id).id
    for _ in range(3):
        current = _follow_up(db, plan.id, current).result["follow_up"]["id"]
    for _ in range(2):
        service.transition_node(current, "active", actor_id="w", db=db)
        submit_review_evidence(current, db=db, actor_id="w", evidence="done")
        _rejected, revision, _replayed = rejection.reject_submission(
            current, feedback="again", actor_id="a", db=db,
        )
        assert _row(db, revision.id)["follow_up_of"] == current
        current = revision.id


def test_the_canonical_creation_event_records_the_lineage(db):
    plan = _plan(db)
    source = _task(db, plan.id)

    created_id = _follow_up(db, plan.id, source.id).result["follow_up"]["id"]

    payload = json.loads(db.execute(
        "SELECT payload_json FROM plan_node_events "
        "WHERE node_id=? AND event='node_created'",
        (created_id,),
    ).fetchone()["payload_json"])
    assert payload["follow_up_of"] == source.id


# --- 3. refusals, each by its own identity ---------------------------------


def test_a_blank_acceptance_criteria_is_refused_before_any_write(db):
    plan = _plan(db)
    source = _task(db, plan.id)
    before = db.execute("SELECT COUNT(*) AS n FROM plan_nodes").fetchone()["n"]

    with pytest.raises(service.PlanError, match="acceptance_criteria is required"):
        _follow_up(db, plan.id, source.id, acceptance_criteria="   ")

    assert db.execute("SELECT COUNT(*) AS n FROM plan_nodes").fetchone()["n"] == before


def test_a_blank_title_is_refused(db):
    plan = _plan(db)
    source = _task(db, plan.id)

    with pytest.raises(service.PlanError, match="title is required"):
        _follow_up(db, plan.id, source.id, title="")


def test_a_source_node_in_another_plan_is_refused(db):
    plan = _plan(db)
    other = _plan(db, "other-root")
    foreign_source = _task(db, other.id, "Elsewhere")

    with pytest.raises(
        PermissionError, match="plan node is outside the requested plan",
    ):
        _follow_up(db, plan.id, foreign_source.id)


def test_a_foreign_actor_cannot_add_a_follow_up(db):
    plan = _plan(db)
    source = _task(db, plan.id)

    with pytest.raises(
        PermissionError, match="plan owner is not accessible to the current actor",
    ):
        _follow_up(db, plan.id, source.id, actor=FOREIGN)


def test_a_stale_revision_is_refused(db):
    plan = _plan(db)
    source = _task(db, plan.id)

    with pytest.raises(service.PlanError, match="plan node revision conflict"):
        _follow_up(db, plan.id, source.id, revision=_row(db, source.id)["revision"] + 5)


def test_an_open_decision_node_refuses_a_follow_up_naming_the_owning_lane(db):
    """A follow-up from an unanswered question presumes an answer the lane holds."""
    decision = raise_decision(db, "root", "root")
    plan_id = _row(db, decision["node_id"])["plan_id"]

    with pytest.raises(
        service.PlanError,
        match="use the decision operations .* instead of add_follow_up",
    ):
        _follow_up(db, plan_id, decision["node_id"])


def test_an_archived_source_node_refuses_a_follow_up(db):
    plan = _plan(db)
    source = _task(db, plan.id)
    db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (source.id,))
    db.commit()

    with pytest.raises(service.PlanError, match="plan node is archived"):
        _follow_up(db, plan.id, source.id)


# --- 4. idempotency and the content-free nudge ------------------------------


def test_a_replayed_follow_up_returns_the_same_node_without_a_second_one(db):
    plan = _plan(db)
    source = _task(db, plan.id)
    mutation_id = _mid()

    first = _follow_up(db, plan.id, source.id, mutation_id=mutation_id).result
    before = db.execute("SELECT COUNT(*) AS n FROM plan_nodes").fetchone()["n"]
    second = _follow_up(
        db, plan.id, source.id, mutation_id=mutation_id,
        revision=_row(db, source.id)["revision"],
    ).result

    assert second["follow_up"]["id"] == first["follow_up"]["id"]
    assert db.execute("SELECT COUNT(*) AS n FROM plan_nodes").fetchone()["n"] == before


def test_one_content_free_nudge_follows_a_committed_follow_up(db):
    plan = _plan(db)
    source = _task(db, plan.id)

    deferred = _follow_up(db, plan.id, source.id, title="Secret residual work")

    assert len(deferred.messages) == 1
    _user, message = deferred.messages[0]
    assert message["type"] == "plans_changed"
    assert "Secret residual work" not in json.dumps(message)


# --- 5. parity across the generic HTTP route and the agent tool -------------


def test_the_generic_http_route_accepts_the_operation(db, monkeypatch):
    from api import plans as api_plans

    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {"id": "user-1"}
    plan = _plan(db)
    source = _task(db, plan.id)

    response = TestClient(app).post(
        f"/api/plans/{plan.id}/mutations",
        json={
            "operation": "add_follow_up",
            "mutation_id": _mid(),
            "params": {
                "node_id": source.id,
                "title": "Residual work",
                "acceptance_criteria": "Residual DOD",
                "expected_revision": _row(db, source.id)["revision"],
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["follow_up"]["follow_up_of"] == source.id


def test_the_agent_tool_exposes_the_operation(db):
    from tools import plans as tool_plans

    assert "add_follow_up" in tool_plans._OPERATIONS
