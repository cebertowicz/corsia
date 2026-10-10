"""Non-recursive `archive_node` through the generic mutation boundary (C2).

The staged P2b instruction tells agents to "prune or archive stale active nodes
**through supported structured mutations**" — and no such mutation exists, so
that instruction ships false. `plan_nodes.archived_at` has exactly one writer
at the time (the markdown importer's whole-plan rollback, since deleted), and every projection already
filters on it, so the column is a supported concept with no supported operation.

C2 adds the operation. It is *not* a delete: the row, its events and its links
survive, and the node leaves the current/operator/rollup/open-count/liveness
projections because those already filter `archived_at IS NULL`.

Three stranding risks were proven against the naive archive at `15a45c9c`, and
each has a test below:

* R1 `node_waits.unmet_prerequisites` does not filter archived prerequisites, so archiving
  a prerequisite blocks its dependent forever on an invisible node.
* node attention survives the archive, stranding a row in the one global pile.
* an armed watch on an archived node stays armed, keeping it waiting forever.

Red at `15a45c9c`: the operation exists at no layer.
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

_DEADLINE = {"type": "deadline", "value": "2099-01-01T00:00:00+00:00"}


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


def _task(db, plan_id: str, title: str = "Stale", **kwargs):
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


def _counts(db) -> dict[str, int]:
    return {
        table: db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in (
            "plan_nodes", "plan_node_dependencies", "plan_node_events",
            "plan_mutation_receipts", "artifact_identities",
            "plan_node_artifact_links", "expected_event_watches",
            "plan_liveness_wakes",
        )
    }


def _generic_attention_count(db, node_id: str) -> int:
    return int(
        db.execute(
            """SELECT COUNT(*) FROM artifact_identities
               WHERE integration='local' AND type='plan-node' AND external_id=?""",
            (node_id,),
        ).fetchone()[0]
    )


def _archive(db, plan_id, node_id, *, actor=OWNER, revision=None, mutation_id=None):
    return mutate(
        plan_id, "archive_node",
        {
            "node_id": node_id,
            "expected_revision": (
                revision if revision is not None else _row(db, node_id)["revision"]
            ),
        },
        actor=actor, provenance="test", mutation_id=mutation_id or _mid(), db=db,
    )


def _depend(db, plan_id, node_id, prerequisite_id, operation="add_dependency"):
    return mutate(
        plan_id, operation,
        {
            "node_id": node_id,
            "depends_on_node_id": prerequisite_id,
            "expected_revision": _row(db, node_id)["revision"],
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )


# --- 1. archive retires a node without deleting it --------------------------


def test_archive_records_the_retirement_with_actor_and_provenance(db):
    plan = _plan(db)
    node = _task(db, plan.id)
    before = _row(db, node.id)["revision"]

    _archive(db, plan.id, node.id)

    archived = _row(db, node.id)
    assert archived["archived_at"] is not None
    assert archived["revision"] == before + 1
    event = db.execute(
        "SELECT * FROM plan_node_events WHERE node_id=? AND event='node_archived'",
        (node.id,),
    ).fetchone()
    assert event["actor_id"] == "root"
    assert event["provenance"] == "test"


def test_archive_is_not_a_delete(db):
    plan = _plan(db)
    node = _task(db, plan.id)
    events_before = _events(db, node.id, "node_created")

    _archive(db, plan.id, node.id)

    assert _row(db, node.id) is not None
    assert _events(db, node.id, "node_created") == events_before


def test_archive_never_fabricates_acceptance(db):
    plan = _plan(db)
    node = _task(db, plan.id)
    service.transition_node(node.id, "active", db=db)
    db.commit()

    _archive(db, plan.id, node.id)

    assert _row(db, node.id)["state"] == "active"
    assert _events(db, node.id, "state_transition") == 1


def test_archive_writes_a_replayable_receipt(db):
    plan = _plan(db)
    node = _task(db, plan.id)
    mutation_id = _mid()

    first = _archive(db, plan.id, node.id, mutation_id=mutation_id).result
    before = _counts(db)
    second = _archive(
        db, plan.id, node.id, mutation_id=mutation_id,
        revision=first["node"]["revision"] - 1,
    ).result

    assert second == first
    assert _counts(db) == before


# --- 2. the node leaves every live projection -------------------------------


#: Every state the 'prune stale active nodes' instruction can encounter, and the
#: transitions that reach it. Terminal states are archivable clutter; open ones
#: are the stale work the instruction actually targets.
_STATES = {
    "pending": (),
    "active": ("active",),
    "review_requested": ("active", "review_requested"),
    "failed": ("active", "failed"),
}


@pytest.mark.parametrize("state", sorted(_STATES))
def test_an_archived_node_leaves_the_progress_and_liveness_projections(db, state):
    from plans import projections

    plan = _plan(db)
    node = _task(db, plan.id)
    for target in _STATES[state]:
        service.transition_node(node.id, target, db=db)
    db.commit()
    assert _row(db, node.id)["state"] == state

    _archive(db, plan.id, node.id)

    assert projections.progress(plan.id, db=db).total == 0
    assert projections.liveness_signal("root", db=db).count == 0


def test_an_archived_node_leaves_the_rollup_and_current_plan_reads(db):
    from plans import current, rollup

    plan = _plan(db)
    node = _task(db, plan.id, "Secret stale work")
    _task(db, plan.id, "Live work")

    _archive(db, plan.id, node.id)

    encoded = json.dumps(rollup.rollup_thread(plan.id, db=db), default=str)
    assert "Secret stale work" not in encoded
    assert "Live work" in encoded
    read = current.read_current_plan(actor=OWNER, db=db)
    assert node.id not in json.dumps(read, default=str)


def test_an_archived_node_frees_its_sibling_order_slot(db):
    plan = _plan(db)
    first = _task(db, plan.id, "First")
    _archive(db, plan.id, first.id)

    later = _task(db, plan.id, "Later")

    assert later.order_key == 0


# --- 3. R1: no archived-prerequisite stranding ------------------------------


def test_archiving_a_prerequisite_takes_its_edge_with_it(db):
    """R1 drops the edge instead of refusing: see test_plan_dependency_retirement.

    The refusal this replaces named ``remove_dependency`` and made the owner
    walk every edge by hand. Dropping the edge in the same write removes the
    stranding the guard protected against, so nothing is left to refuse.
    """
    plan = _plan(db)
    dependent = _task(db, plan.id, "Dependent")
    prerequisite = _task(db, plan.id, "Prerequisite")
    _depend(db, plan.id, dependent.id, prerequisite.id)

    _archive(db, plan.id, prerequisite.id)

    assert _row(db, prerequisite.id)["archived_at"] is not None
    assert node_waits.waits_on_dependency(db, dependent.id, "pending") is False


def test_archiving_the_dependent_end_takes_its_edge_too(db):
    plan = _plan(db)
    dependent = _task(db, plan.id, "Dependent")
    prerequisite = _task(db, plan.id, "Prerequisite")
    _depend(db, plan.id, dependent.id, prerequisite.id)

    _archive(db, plan.id, dependent.id)

    assert _row(db, dependent.id)["archived_at"] is not None
    assert db.execute(
        "SELECT 1 FROM plan_node_dependencies WHERE node_id=?", (dependent.id,)
    ).fetchone() is None


def test_removing_the_edge_first_leaves_no_blocked_dependent(db):
    plan = _plan(db)
    dependent = _task(db, plan.id, "Dependent")
    prerequisite = _task(db, plan.id, "Prerequisite")
    _depend(db, plan.id, dependent.id, prerequisite.id)
    assert node_waits.waits_on_dependency(db, dependent.id, "pending") is True

    _depend(db, plan.id, dependent.id, prerequisite.id, "remove_dependency")
    _archive(db, plan.id, prerequisite.id)

    assert node_waits.waits_on_dependency(db, dependent.id, "pending") is False
    assert _generic_attention_count(db, dependent.id) == 0


# --- 4. R2/R3: attention and watches are settled, not stranded --------------


def test_archive_does_not_create_generic_node_attention(db):
    plan = _plan(db)
    node = _task(db, plan.id)

    _archive(db, plan.id, node.id)

    assert _generic_attention_count(db, node.id) == 0


def test_archive_resolves_an_armed_watch_instead_of_expiring_it(db):
    """`expire_watch` would route a false delivery_failure; archive must not."""
    plan = _plan(db)
    node = _task(db, plan.id)
    service.transition_node(node.id, "active", db=db)
    db.commit()
    mutate(
        plan.id, "arm_watch",
        {
            "node_id": node.id,
            "expected_revision": _row(db, node.id)["revision"],
            "condition": _DEADLINE,
            "expected_event": "external.done",
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )
    armed = "SELECT COUNT(*) FROM expected_event_watches WHERE plan_node_id=? AND status='armed'"
    assert db.execute(armed, (node.id,)).fetchone()[0] == 1

    _archive(db, plan.id, node.id)

    assert db.execute(armed, (node.id,)).fetchone()[0] == 0
    statuses = [
        row["status"] for row in db.execute(
            "SELECT status FROM expected_event_watches WHERE plan_node_id=?",
            (node.id,),
        ).fetchall()
    ]
    assert statuses == ["resolved"]
    assert db.execute(
        "SELECT COUNT(*) AS n FROM expected_event_watches WHERE routed_at IS NOT NULL",
    ).fetchone()["n"] == 0


def test_a_stale_plan_wake_self_cancels_after_the_last_node_is_archived(db):
    from automation import plan_liveness
    from automation.continuation.sources import liveness as liveness_source

    plan = _plan(db)
    node = _task(db, plan.id)
    mutate(
        plan.id, "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )
    plan_liveness.on_turn_complete("root", db=db)
    db.commit()

    _archive(db, plan.id, node.id)

    requests = liveness_source.LivenessSource().due("2999-01-01T00:00:00+00:00", db=db)
    assert [request.reason for request in requests] == ["no_actionable_work"]


# --- 5. structural safety: children, delegation, lanes ----------------------


def test_archive_is_not_recursive_and_refuses_unarchived_children(db):
    plan = _plan(db)
    track = service.add_node(
        plan.id, kind="track", title="Track", acceptance_criteria="Track DOD", db=db,
    )
    db.commit()
    child = _task(db, plan.id, "Child", parent_id=track.id)

    with pytest.raises(service.PlanError, match="children"):
        _archive(db, plan.id, track.id)
    db.rollback()

    assert _row(db, track.id)["archived_at"] is None
    assert _row(db, child.id)["archived_at"] is None


def test_archiving_the_children_first_lets_the_parent_be_archived(db):
    plan = _plan(db)
    track = service.add_node(
        plan.id, kind="track", title="Track", acceptance_criteria="Track DOD", db=db,
    )
    db.commit()
    child = _task(db, plan.id, "Child", parent_id=track.id)

    _archive(db, plan.id, child.id)
    _archive(db, plan.id, track.id)

    assert _row(db, track.id)["archived_at"] is not None


def test_archive_refuses_a_node_whose_spawned_thread_still_has_open_work(db):
    from plans import mutations

    plan = _plan(db)
    node = _task(db, plan.id)
    service.transition_node(node.id, "active", db=db)
    db.commit()
    mutations.attach_spawned_thread(
        node.id, "child", expected_revision=_row(db, node.id)["revision"],
        actor_id="root", provenance="test", db=db,
    )
    child_plan = _plan(db, "child")
    _task(db, child_plan.id, "Child work")

    with pytest.raises(service.PlanError, match="spawned"):
        _archive(db, plan.id, node.id)
    db.rollback()

    assert _row(db, node.id)["archived_at"] is None


def test_archive_refuses_an_open_decision_node_naming_the_owning_lane(db):
    decision = raise_decision(db, "root", "root")
    plan_id = _row(db, decision["node_id"])["plan_id"]

    with pytest.raises(
        service.PlanError,
        match="use the decision operations .* instead of archive_node",
    ):
        _archive(db, plan_id, decision["node_id"])
    db.rollback()

    assert _row(db, decision["node_id"])["archived_at"] is None


def test_an_invalidated_decision_node_can_be_archived(db):
    from plans import decisions

    decision = raise_decision(db, "root", "root")
    decisions.invalidate(
        decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
        actor=OWNER, provenance="p", db=db,
    )
    db.commit()
    plan_id = _row(db, decision["node_id"])["plan_id"]

    _archive(db, plan_id, decision["node_id"])

    assert _row(db, decision["node_id"])["archived_at"] is not None


# --- 6. refusals by identity ------------------------------------------------


def test_a_second_archive_is_refused_as_already_archived(db):
    plan = _plan(db)
    node = _task(db, plan.id)
    _archive(db, plan.id, node.id)

    with pytest.raises(service.PlanError, match="plan node is archived"):
        _archive(db, plan.id, node.id)


def test_a_node_in_another_plan_is_refused(db):
    plan = _plan(db)
    other = _plan(db, "other-root")
    elsewhere = _task(db, other.id, "Elsewhere")

    with pytest.raises(
        PermissionError, match="plan node is outside the requested plan",
    ):
        _archive(db, plan.id, elsewhere.id)


def test_a_foreign_actor_cannot_archive(db):
    plan = _plan(db)
    node = _task(db, plan.id)

    with pytest.raises(
        PermissionError, match="plan owner is not accessible to the current actor",
    ):
        _archive(db, plan.id, node.id, actor=FOREIGN)


def test_a_stale_revision_is_refused(db):
    plan = _plan(db)
    node = _task(db, plan.id)

    with pytest.raises(service.PlanError, match="plan node revision conflict"):
        _archive(db, plan.id, node.id, revision=_row(db, node.id)["revision"] + 5)


def test_an_unknown_node_is_refused(db):
    plan = _plan(db)

    with pytest.raises(service.PlanError, match="unknown plan node"):
        _archive(db, plan.id, "missing-node", revision=1)


# --- 7. an archived node is inert ------------------------------------------


@pytest.mark.parametrize(
    "operation,extra",
    [
        ("activate", {}),
        ("edit", {"title": "Renamed"}),
        ("reorder", {"order_key": 0}),
        ("request_review", {}),
    ],
)
def test_an_archived_node_accepts_no_further_mutation(db, operation, extra):
    plan = _plan(db)
    node = _task(db, plan.id)
    _archive(db, plan.id, node.id)

    with pytest.raises(service.PlanError, match="plan node is archived"):
        mutate(
            plan.id, operation,
            {
                "node_id": node.id,
                "expected_revision": _row(db, node.id)["revision"],
                **extra,
            },
            actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
        )


def test_an_archived_node_accepts_no_new_dependency(db):
    """C1 already refuses an archived endpoint, behind its non-disclosing message."""
    from plans import dependencies

    plan = _plan(db)
    node = _task(db, plan.id, "Dependent")
    prerequisite = _task(db, plan.id, "Prerequisite")
    _archive(db, plan.id, prerequisite.id)

    with pytest.raises(service.PlanError, match=dependencies._DEPENDENCY_REFUSED):
        _depend(db, plan.id, node.id, prerequisite.id)


def test_an_archived_node_accepts_no_child(db):
    plan = _plan(db)
    track = service.add_node(
        plan.id, kind="track", title="Track", acceptance_criteria="Track DOD", db=db,
    )
    db.commit()
    _archive(db, plan.id, track.id)

    with pytest.raises(service.PlanError, match="node parent is archived"):
        service.add_node(
            plan.id, kind="task", title="Child", acceptance_criteria="Child DOD",
            parent_id=track.id, db=db,
        )


# --- 8. atomicity and the content-free nudge -------------------------------


@pytest.mark.parametrize("boundary", ["event", "receipt"])
def test_a_failure_at_any_boundary_leaves_the_node_live(db, monkeypatch, boundary):
    from plans import receipts

    plan = _plan(db)
    node = _task(db, plan.id)
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
        _archive(db, plan.id, node.id)

    monkeypatch.undo()
    assert _counts(db) == before
    assert _row(db, node.id)["archived_at"] is None


def test_one_content_free_nudge_follows_a_committed_archive(db):
    plan = _plan(db)
    node = _task(db, plan.id, "Secret stale work")

    deferred = _archive(db, plan.id, node.id)

    assert len(deferred.messages) == 1
    _user, message = deferred.messages[0]
    assert message["type"] == "plans_changed"
    assert "Secret stale work" not in json.dumps(message)


# --- 9. parity across the generic HTTP route and the agent tool -------------


def test_the_generic_http_route_accepts_the_operation(db, monkeypatch):
    from api import plans as api_plans

    monkeypatch.setattr(api_plans, "get_db", lambda: _DbProxy(db))
    monkeypatch.setattr(api_plans, "run_in_db", _immediate)
    app = FastAPI()
    app.include_router(api_plans.router)
    app.dependency_overrides[api_plans.require_user] = lambda: {"id": "user-1"}
    plan = _plan(db)
    node = _task(db, plan.id)

    response = TestClient(app).post(
        f"/api/plans/{plan.id}/mutations",
        json={
            "operation": "archive_node",
            "mutation_id": _mid(),
            "params": {
                "node_id": node.id,
                "expected_revision": _row(db, node.id)["revision"],
            },
        },
    )

    assert response.status_code == 200
    assert _row(db, node.id)["archived_at"] is not None


def test_the_agent_tool_exposes_the_operation(db):
    from tools import plans as tool_plans

    assert "archive_node" in tool_plans._OPERATIONS


# --- 10. the delegation attachment: archive may not end a live delegation ---
#
# A node owns a live delegation exactly while it is unarchived AND its own state
# is open. C2 applied its own refusal rule to children, dependency edges and
# open child-plan work, but not to the attachment itself; and the five
# predicates that read attachments were already missing the archive half of
# their condition before C2 existed. Together those produced an archived,
# open-state, delegated node that no projection shows and that permanently
# refuses re-delegating the child.


def _submit_review_evidence(db, node_id):
    from plans import review_evidence

    return review_evidence.submit(
        node_id,
        evidence="Focused proof passed",
        expected_revision=service.get_node(node_id, db=db).revision,
        actor_id="worker",
        provenance="test.plan_archival",
        db=db,
    )


def _delegated(db, *, state="active", child="child"):
    """One plan node holding a live attachment to a spawned child thread."""
    from plans import mutations

    plan = _plan(db)
    node = _task(db, plan.id, "Delegate")
    if state != "pending":
        service.transition_node(node.id, "active", db=db)
    if state == "review_requested":
        service.transition_node(node.id, "review_requested", db=db)
    db.commit()
    mutations.attach_spawned_thread(node.id, child, db=db)
    db.commit()
    return plan, service.get_node(node.id, db=db)


def _force_archive(db, node_id: str) -> None:
    """Archive exactly as any foreign writer does: archived_at, never state."""
    db.execute(
        "UPDATE plan_nodes SET archived_at=?, revision=revision+1 WHERE id=?",
        (service._now(), node_id),
    )
    db.commit()


@pytest.mark.parametrize("state", ["pending", "active", "review_requested"])
def test_archive_refuses_a_node_still_holding_a_live_delegation(db, state):
    """R1. The child plan is empty, so only the attachment blocks the archive."""
    plan, node = _delegated(db, state=state)

    with pytest.raises(service.PlanError, match="harvest"):
        _archive(db, plan.id, node.id)
    db.rollback()

    assert _row(db, node.id)["archived_at"] is None


def test_archive_refuses_while_a_finished_child_is_unharvested(db):
    """R7. The A11 review obligation survives: the parent still owes a verdict."""
    plan, node = _delegated(db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    child_task = _task(db, child_plan.id, "Child work")
    service.transition_node(child_task.id, "active", db=db)
    _submit_review_evidence(db, child_task.id)
    service.transition_node(
        child_task.id, "accepted", verification_result="passed",
        verified_by="root", db=db,
    )
    db.commit()

    with pytest.raises(service.PlanError, match="harvest"):
        _archive(db, plan.id, node.id)
    db.rollback()

    assert _row(db, node.id)["archived_at"] is None
    assert _events(db, node.id, "child_plan_completed") == 0


def test_settling_the_delegation_first_lets_the_node_be_archived(db):
    """The refusal names a reachable settlement, so no permanent blocker exists."""
    plan, node = _delegated(db)
    service.transition_node(node.id, "failed", db=db)
    db.commit()

    _archive(db, plan.id, node.id)

    archived = _row(db, node.id)
    assert archived["archived_at"] is not None
    assert archived["spawned_thread_id"] == "child"


@pytest.mark.parametrize("state", ["active", "review_requested"])
def test_a_foreign_archive_does_not_strand_the_child_forever(db, state):
    """R2/R3. Any other archived_at writer must not create a permanent blocker."""
    from plans import mutations

    plan, node = _delegated(db, state=state)
    _force_archive(db, node.id)
    successor = _task(db, plan.id, "Delegate again")
    service.transition_node(successor.id, "active", db=db)
    db.commit()

    mutations.attach_spawned_thread(successor.id, "child", db=db)
    db.commit()

    assert _row(db, successor.id)["spawned_thread_id"] == "child"


def test_an_archived_row_never_owns_p4_completion(db, monkeypatch):
    """R4. `has_open_structured_attachment` must not see an archived node."""
    from plans import delegation

    _plan_row, node = _delegated(db)
    assert delegation.has_open_structured_attachment("child", db=db) is True

    _force_archive(db, node.id)

    assert delegation.has_open_structured_attachment("child", db=db) is False


def test_harvest_never_transitions_an_archived_node(db, monkeypatch):
    """R6. Harvest drove an archived node to review_requested at head."""
    from plans import delegation

    plan, node = _delegated(db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    child_task = _task(db, child_plan.id, "Child work")
    service.transition_node(child_task.id, "active", db=db)
    _submit_review_evidence(db, child_task.id)
    service.transition_node(
        child_task.id, "accepted", verification_result="passed",
        verified_by="root", db=db,
    )
    _force_archive(db, node.id)

    harvested = delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[])
    db.commit()

    assert node.id not in harvested
    assert _row(db, node.id)["state"] == "active"
    assert _events(db, node.id, "child_plan_completed") == 0
    assert _events(db, node.id, "state_transition") == 1


def test_an_old_epoch_completion_cannot_settle_a_new_attachment(db, monkeypatch):
    """The floor still holds after the archived epoch is replaced."""
    from plans import delegation, mutations

    plan, first = _delegated(db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    stale = _task(db, child_plan.id, "Old worker result")
    service.transition_node(stale.id, "active", db=db)
    _submit_review_evidence(db, stale.id)
    service.transition_node(
        stale.id, "accepted", verification_result="passed", verified_by="root", db=db,
    )
    _force_archive(db, first.id)
    second = _task(db, plan.id, "Delegate again")
    service.transition_node(second.id, "active", db=db)
    db.commit()
    mutations.attach_spawned_thread(second.id, "child", db=db)
    db.commit()

    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == []
    assert _row(db, second.id)["state"] == "active"
    assert _row(db, first.id)["state"] == "active"
