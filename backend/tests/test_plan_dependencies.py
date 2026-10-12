"""Dependency operations through the generic mutation boundary (C1).

`plan_node_dependencies` has existed since P1 with its triggers and indexes,
`node_waits` derives `unmet_dependencies` from it, and `material_events` computes
blocked deltas from it — but **no production caller can write a row into it**.
So PRD A3 `depends_on[]`, A4 "`blocked` derived", PRD B2 / PLANS-AC11 one-level
block escalation, and PLANS-AC19 blocked-path disclosure are all unreachable:
they render an always-empty set.

`service.add_dependency` is already correct. C1 is exposure plus a symmetric
removal, so these tests assert the *reachable* behaviour and the removal half.

Red at `8826029d`: neither operation exists at the boundary, and
`remove_dependency` exists at no layer.
"""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.schema import SCHEMA_SQL
from plans import service
from plans.boundary import PlanActor, mutate
from tests.test_attention_queue import _mid, _seed
from tests.test_attention_queue_api import _DbProxy, _immediate
from tests.plan_review_helpers import submit_review_evidence
from tests.plan_writer_authorizer import raw_plan_seed

OWNER = PlanActor("root", "user-1", "project-1")
HUMAN = PlanActor(None, "user-1", None, kind="user")


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


def _task(db, plan_id: str, title: str):
    node = service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} DOD", db=db,
    )
    db.commit()
    return node


def _pair(db):
    """One plan, one dependent node and one prerequisite."""
    plan = _plan(db)
    return plan, _task(db, plan.id, "Dependent"), _task(db, plan.id, "Prerequisite")


def _node(db, node_id: str):
    return db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()


def _blocked(db, node_id: str) -> bool:
    from plans import node_waits

    row = _node(db, node_id)
    return node_waits.waits_on_dependency(db, node_id, row["state"])


def _events(db, node_id: str, event: str) -> int:
    return db.execute(
        "SELECT COUNT(*) AS n FROM plan_node_events WHERE node_id=? AND event=?",
        (node_id, event),
    ).fetchone()["n"]


def _edges(db) -> int:
    return db.execute(
        "SELECT COUNT(*) AS n FROM plan_node_dependencies",
    ).fetchone()["n"]


def test_add_creates_its_dependency_in_the_same_mutation(db):
    plan = _plan(db)
    prerequisite = _task(db, plan.id, "Prerequisite")

    created = mutate(plan.id, "add", {
        "title": "Dependent", "acceptance_criteria": "Dependent DOD",
        "depends_on_node_id": prerequisite.id,
    }, actor=OWNER, provenance="test", mutation_id=_mid(), db=db).result["node"]

    assert _edges(db) == 1
    assert _blocked(db, created["id"])
    assert _events(db, created["id"], "dependency_added") == 1


def _attention_raised(db, node_id: str) -> bool:
    row = db.execute(
        """SELECT metadata FROM artifact_identities
           WHERE integration='local' AND type='plan-node' AND external_id=?""",
        (node_id,),
    ).fetchone()
    if row is None or not row["metadata"]:
        return False
    import json

    return json.loads(row["metadata"]).get("attention_state") == "raised"


def _counts(db) -> dict[str, int]:
    return {
        table: db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in (
            "plan_node_dependencies", "plan_node_events", "plan_mutation_receipts",
            "artifact_identities", "plan_node_artifact_links",
        )
    }


def _depend(db, plan_id, node, prerequisite, *, actor=OWNER,
            operation="add_dependency", revision=None, mutation_id=None):
    return mutate(
        plan_id, operation,
        {
            "node_id": node["id"] if isinstance(node, sqlite3.Row) else node.id,
            "depends_on_node_id": (
                prerequisite["id"] if isinstance(prerequisite, sqlite3.Row)
                else prerequisite.id
            ),
            "expected_revision": (
                revision if revision is not None
                else _node(db, node.id if hasattr(node, "id") else node["id"])["revision"]
            ),
        },
        actor=actor, provenance="test", mutation_id=mutation_id or _mid(), db=db,
    )


def _client(db, monkeypatch, user_id: str = "user-1") -> TestClient:
    from api import plans as api_plans

    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {"id": user_id}
    return TestClient(app)


# --- 1. the operation is reachable at all -----------------------------------


def test_a_dependency_makes_the_dependent_blocked(db):
    """PRD A4: `blocked` is derived, and until now nothing could derive it."""
    plan, node, prerequisite = _pair(db)
    assert _blocked(db, node.id) is False

    _depend(db, plan.id, node, prerequisite)
    db.commit()

    assert _blocked(db, node.id) is True
    assert _edges(db) == 1


def test_the_blocked_delta_settles_exactly_once(db):
    """One blocked event; the Plans wake reads it without generic attention."""
    plan, node, prerequisite = _pair(db)

    _depend(db, plan.id, node, prerequisite)
    db.commit()

    assert _events(db, node.id, "dependency_added") == 1
    assert _events(db, node.id, "blocked_raised") == 1
    assert _attention_raised(db, node.id) is False


def test_a_second_prerequisite_does_not_re_raise_an_already_blocked_node(db):
    plan, node, first = _pair(db)
    second = _task(db, plan.id, "Second prerequisite")
    _depend(db, plan.id, node, first)
    db.commit()

    _depend(db, plan.id, node, second)
    db.commit()

    assert _edges(db) == 2
    # Already blocked, so the delta is false→false: no second event, no re-raise.
    assert _events(db, node.id, "blocked_raised") == 1


def test_a_duplicate_edge_adds_no_event_attention_or_wake(db):
    plan, node, prerequisite = _pair(db)
    _depend(db, plan.id, node, prerequisite)
    db.commit()
    before = _counts(db)

    _depend(db, plan.id, node, prerequisite)
    db.commit()

    after = _counts(db)
    assert after["plan_node_dependencies"] == before["plan_node_dependencies"]
    assert after["plan_node_events"] == before["plan_node_events"]


# --- 2. the prerequisite clearing the block ---------------------------------


def test_accepting_the_prerequisite_clears_the_derived_block(db):
    """The existing material-event path, now reachable from a real dependency."""
    plan, node, prerequisite = _pair(db)
    _depend(db, plan.id, node, prerequisite)
    db.commit()
    assert _blocked(db, node.id) is True

    service.transition_node(
        prerequisite.id, "active", actor_id="root", provenance="test", db=db,
    )
    submit_review_evidence(prerequisite.id, db=db, actor_id="root")
    service.transition_node(
        prerequisite.id, "accepted", actor_id="root", provenance="test",
        verification_result="passed", verified_by="reviewer", db=db,
    )
    db.commit()

    assert _blocked(db, node.id) is False
    assert _events(db, node.id, "blocked_cleared") == 1
    assert _attention_raised(db, node.id) is False


# --- 3. removal is symmetric ------------------------------------------------


def test_removing_the_only_dependency_clears_the_block(db):
    plan, node, prerequisite = _pair(db)
    _depend(db, plan.id, node, prerequisite)
    db.commit()

    _depend(db, plan.id, node, prerequisite, operation="remove_dependency")
    db.commit()

    assert _edges(db) == 0
    assert _blocked(db, node.id) is False
    assert _events(db, node.id, "dependency_removed") == 1
    assert _events(db, node.id, "blocked_cleared") == 1
    assert _attention_raised(db, node.id) is False


def test_removing_one_of_two_dependencies_fires_no_false_clear(db):
    """The risk the inventory names: removal must snapshot the same node set."""
    plan, node, first = _pair(db)
    second = _task(db, plan.id, "Second prerequisite")
    _depend(db, plan.id, node, first)
    _depend(db, plan.id, node, second)
    db.commit()

    _depend(db, plan.id, node, first, operation="remove_dependency")
    db.commit()

    assert _edges(db) == 1
    assert _blocked(db, node.id) is True
    assert _events(db, node.id, "blocked_cleared") == 0
    assert _attention_raised(db, node.id) is False


def test_removing_an_absent_edge_changes_no_plan_state(db):
    """A no-op still consumes its mutation_id — that is what makes a replay
    after a lost response return the same answer — but writes no plan state."""
    plan, node, prerequisite = _pair(db)
    before = _counts(db)

    _depend(db, plan.id, node, prerequisite, operation="remove_dependency")
    db.commit()

    after = _counts(db)
    assert after["plan_node_dependencies"] == before["plan_node_dependencies"]
    assert after["plan_node_events"] == before["plan_node_events"]
    assert after["plan_node_artifact_links"] == before["plan_node_artifact_links"]
    assert _blocked(db, node.id) is False


def test_removing_an_already_satisfied_dependency_causes_no_transition(db):
    plan, node, prerequisite = _pair(db)
    _depend(db, plan.id, node, prerequisite)
    service.transition_node(
        prerequisite.id, "active", actor_id="root", provenance="test", db=db,
    )
    submit_review_evidence(prerequisite.id, db=db, actor_id="root")
    service.transition_node(
        prerequisite.id, "accepted", actor_id="root", provenance="test",
        verification_result="passed", verified_by="reviewer", db=db,
    )
    db.commit()
    state_before = _node(db, node.id)["state"]
    cleared_before = _events(db, node.id, "blocked_cleared")

    _depend(db, plan.id, node, prerequisite, operation="remove_dependency")
    db.commit()

    assert _node(db, node.id)["state"] == state_before
    # Already unblocked, so removal is a true→true no-op on the derived state.
    assert _events(db, node.id, "blocked_cleared") == cleared_before


# --- 4. refusals, before any write and without disclosure -------------------


@pytest.mark.parametrize("operation", ["add_dependency", "remove_dependency"])
def test_a_self_edge_is_refused(db, operation):
    plan, node, _prerequisite = _pair(db)
    before = _counts(db)

    with pytest.raises(service.PlanError):
        _depend(db, plan.id, node, node, operation=operation)
    db.rollback()

    assert _counts(db) == before


def test_a_cycle_is_refused(db):
    plan, first, second = _pair(db)
    _depend(db, plan.id, first, second)
    db.commit()
    before = _counts(db)

    with pytest.raises(service.PlanError):
        _depend(db, plan.id, second, first)
    db.rollback()

    assert _counts(db) == before


@pytest.mark.parametrize("operation", ["add_dependency", "remove_dependency"])
def test_an_unreadable_plan_prerequisite_is_refused_without_disclosure(db, operation):
    """Another plan's task is refused unless the caller could read that plan (#1552)."""
    plan, node, _prerequisite = _pair(db)
    other = _plan(db, "other-root")
    elsewhere = _task(db, other.id, "Elsewhere")
    db.execute("UPDATE threads SET project_id=NULL WHERE id='other-root'")
    db.commit()
    before = _counts(db)

    with pytest.raises((service.PlanError, PermissionError)) as excinfo:
        _depend(db, plan.id, node, elsewhere, operation=operation)
    db.rollback()

    assert elsewhere.id not in str(excinfo.value)
    assert _counts(db) == before


@pytest.mark.parametrize("operation", ["add_dependency", "remove_dependency"])
def test_an_unknown_prerequisite_is_refused_without_disclosure(db, operation):
    plan, node, _prerequisite = _pair(db)
    before = _counts(db)

    class Missing:
        id = "no-such-node"

    with pytest.raises((service.PlanError, PermissionError)) as excinfo:
        _depend(db, plan.id, node, Missing, operation=operation)
    db.rollback()

    assert "no-such-node" not in str(excinfo.value)
    assert _counts(db) == before


@pytest.mark.parametrize("operation", ["add_dependency", "remove_dependency"])
def test_an_archived_node_is_refused(db, operation):
    plan, node, prerequisite = _pair(db)
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET archived_at='t' WHERE id=?", (prerequisite.id,),
        )
    db.commit()
    before = _counts(db)

    with pytest.raises((service.PlanError, PermissionError)):
        _depend(db, plan.id, node, prerequisite, operation=operation)
    db.rollback()

    assert _counts(db) == before


def test_a_foreign_thread_actor_cannot_add_a_dependency(db):
    plan, node, prerequisite = _pair(db)
    before = _counts(db)

    with pytest.raises(PermissionError):
        _depend(
            db, plan.id, node, prerequisite,
            actor=PlanActor("other-root", "user-1", "project-1"),
        )
    db.rollback()

    assert _counts(db) == before


def test_a_scope_plan_dependency_is_refused(db):
    """Public scope mutation stays denied; dependencies add no exception."""
    scope = service.ensure_plan("project_scope", "project-1", db=db)
    node = _task(db, scope.id, "Scope work")
    prerequisite = _task(db, scope.id, "Scope prerequisite")
    before = _counts(db)

    with pytest.raises(PermissionError):
        _depend(db, scope.id, node, prerequisite, actor=HUMAN)
    db.rollback()

    assert _counts(db) == before


# --- 5. positioning, not settlement -----------------------------------------


def test_dependency_operations_are_not_settlement_operations(db):
    """They position a node in the graph; they never drive or end its state."""
    from plans import node_settlement

    assert "add_dependency" not in node_settlement.SETTLING_OPERATIONS
    assert "remove_dependency" not in node_settlement.SETTLING_OPERATIONS


def test_a_lane_owned_review_node_still_accepts_positioning(db):
    """Matches `reorder`: the lane owns settlement, not the node's position.

    The accepted boundary semantics allow positioning on an open lane-owned
    node, so a dependency — which changes only derived `blocked` — is allowed
    for the same reason. Asserted explicitly rather than assumed.
    """
    from tests.test_attention_queue import raise_decision

    decision = raise_decision(db, "root", "root")
    prerequisite = _task(db, decision["plan_id"], "Prerequisite")

    mutate(
        decision["plan_id"], "add_dependency",
        {
            "node_id": decision["node_id"],
            "depends_on_node_id": prerequisite.id,
            "expected_revision": _node(db, decision["node_id"])["revision"],
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )
    db.commit()

    assert _edges(db) == 1
    # The lane's own settlement state is untouched by positioning.
    assert _node(db, decision["node_id"])["state"] == "review_requested"


# --- 6. concurrency, revision and replay ------------------------------------


@pytest.mark.parametrize("operation", ["add_dependency", "remove_dependency"])
def test_a_stale_expected_revision_is_refused(db, operation):
    plan, node, prerequisite = _pair(db)
    before = _counts(db)

    with pytest.raises(service.PlanError):
        _depend(db, plan.id, node, prerequisite, operation=operation,
                revision=_node(db, node.id)["revision"] + 5)
    db.rollback()

    assert _counts(db) == before


def test_replaying_one_mutation_id_is_idempotent(db):
    plan, node, prerequisite = _pair(db)
    mutation_id = _mid()

    first = _depend(db, plan.id, node, prerequisite, mutation_id=mutation_id)
    db.commit()
    after_first = _counts(db)
    second = _depend(db, plan.id, node, prerequisite, mutation_id=mutation_id)
    db.commit()

    assert first.result == second.result
    assert _counts(db) == after_first
    assert first.messages != ()
    assert second.messages == ()


def test_add_then_remove_leaves_one_edge_history_and_no_edge(db):
    plan, node, prerequisite = _pair(db)

    _depend(db, plan.id, node, prerequisite)
    _depend(db, plan.id, node, prerequisite, operation="remove_dependency")
    db.commit()

    assert _edges(db) == 0
    assert _events(db, node.id, "dependency_added") == 1
    assert _events(db, node.id, "dependency_removed") == 1


@pytest.mark.parametrize("boundary", ["event", "receipt"])
def test_a_failure_at_any_boundary_leaves_no_partial_edge(db, monkeypatch, boundary):
    from plans import receipts

    plan, node, prerequisite = _pair(db)
    before = _counts(db)

    def explode(*_args, **_kwargs):
        raise RuntimeError(f"injected {boundary} failure")

    targets = {
        "event": (service, "_event"),
        "receipt": (receipts, "record"),
    }
    module, name = targets[boundary]
    monkeypatch.setattr(module, name, explode)

    with pytest.raises(RuntimeError):
        _depend(db, plan.id, node, prerequisite)

    monkeypatch.undo()
    assert _counts(db) == before
    assert _blocked(db, node.id) is False


def test_one_content_free_nudge_after_a_committed_dependency(db):
    plan, node, prerequisite = _pair(db)

    deferred = _depend(db, plan.id, node, prerequisite)

    assert len(deferred.messages) == 1
    _user, message = deferred.messages[0]
    assert message["type"] == "plans_changed"
    import json

    encoded = json.dumps(message)
    for secret in ("Dependent", "Prerequisite"):
        assert secret not in encoded


# --- 7. parity through the generic HTTP route and the agent tool ------------


def test_the_generic_http_route_accepts_both_operations(db, monkeypatch):
    plan, node, prerequisite = _pair(db)
    client = _client(db, monkeypatch)

    added = client.post(
        f"/api/plans/{plan.id}/mutations",
        json={"operation": "add_dependency", "mutation_id": _mid(),
              "params": {"node_id": node.id, "depends_on_node_id": prerequisite.id,
                         "expected_revision": _node(db, node.id)["revision"]}},
    )
    assert added.status_code == 200, added.text
    assert _blocked(db, node.id) is True

    removed = client.post(
        f"/api/plans/{plan.id}/mutations",
        json={"operation": "remove_dependency", "mutation_id": _mid(),
              "params": {"node_id": node.id, "depends_on_node_id": prerequisite.id,
                         "expected_revision": _node(db, node.id)["revision"]}},
    )
    assert removed.status_code == 200, removed.text
    assert _blocked(db, node.id) is False


def test_the_agent_tool_publishes_both_operations(db):
    from tools import plans as tool_plans

    operations = tool_plans.PlanUpdateTool().parameters["properties"]["operation"]["enum"]

    assert "add_dependency" in operations
    assert "remove_dependency" in operations


@pytest.mark.parametrize("operation", ["add_dependency", "remove_dependency"])
def test_cross_operation_parameters_are_rejected(db, operation):
    plan, node, prerequisite = _pair(db)

    with pytest.raises(service.PlanError):
        mutate(
            plan.id, operation,
            {"node_id": node.id, "depends_on_node_id": prerequisite.id,
             "expected_revision": _node(db, node.id)["revision"], "title": "nope"},
            actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
        )
    db.rollback()


# --- 8. the projection the block finally reaches ----------------------------


def test_the_operator_projection_shows_the_blocked_path_from_canonical_state(db):
    """PLANS-AC19: the blocked path stops being an always-empty set."""
    from plans import operator_projection

    plan = _plan(db)
    parent = service.add_node(
        plan.id, kind="phase", title="Phase", acceptance_criteria="Phase DOD", db=db,
    )
    node = service.add_node(
        plan.id, kind="task", title="Dependent", acceptance_criteria="DOD",
        parent_id=parent.id, db=db,
    )
    prerequisite = _task(db, plan.id, "Prerequisite")
    db.commit()
    _depend(db, plan.id, node, prerequisite)
    db.commit()

    envelope = operator_projection.read_thread("root", actor=HUMAN, db=db)

    presented = {item["id"]: item for item in envelope["plans"][0]["nodes"]}
    assert list(presented[node.id]["unmet_dependencies"]) == [prerequisite.id]
    assert presented[node.id]["attention_path"] == [parent.id, node.id]
