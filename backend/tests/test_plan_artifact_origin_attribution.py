"""Artifact origin follows the executor's canonical selection, not Turn accounting.

``plans.admission_origin.registration_origin`` resolves the node an artifact
registration binds to from the thread's valid selection in its own plan
when one exists, and only otherwise from the Turn's write-once accounting
snapshot, which never binds an archived or terminal node. The Turn identity
checks (unknown Turn, wrong thread, not live) and their reasons run first.

Every registration goes through ``registry.models.register_artifact`` with a
trusted ``RegistrationOrigin``; every plan transition that matters goes through
``plans.boundary.mutate``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from infra.database import get_db
from infra.turn_plan_binding import bind_turn_to_plan_attempt
from plans import admission, review_usage, service
from plans.boundary import PlanActor, mutate
from plans.models import PlanNode
from registry import models
from registry.registration_writer import RegistrationOrigin
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import add_task, finish_turn, open_turn
from tests.registry_helpers import make_session
from tests.test_plan_tool_dispatch_attribution import leave_attempt_in_plan

_PROVENANCE = "test.plan_artifact_origin"


def _actor(db, thread_id: str) -> PlanActor:
    """Build the boundary actor from the thread's persisted identity."""
    row = db.execute(
        "SELECT user_id,project_id FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    return PlanActor(thread_id, row["user_id"], row["project_id"])


def _mutate(db, actor_thread: str, plan_id: str, operation: str, params: dict) -> dict:
    """Run one plan mutation through the actor-bound boundary."""
    return mutate(
        plan_id, operation, params,
        actor=_actor(db, actor_thread),
        provenance=_PROVENANCE,
        mutation_id=f"{operation}-{params['node_id']}",
        db=db,
    ).result


def _request_review(db, actor_thread: str, plan_id: str, node_id: str) -> dict:
    """Submit review evidence for the node's current revision."""
    node = service.get_node(node_id, db=db)
    return _mutate(db, actor_thread, plan_id, "request_review", {
        "node_id": node_id,
        "evidence": "Focused checks passed",
        "expected_revision": node.revision,
    })


def _accept(db, actor_thread: str, plan_id: str, node_id: str) -> dict:
    """Accept reviewed work at its current revision."""
    node = service.get_node(node_id, db=db)
    return _mutate(db, actor_thread, plan_id, "accept", {
        "node_id": node_id,
        "verification_result": "passed",
        "expected_revision": node.revision,
    })


def _activate(db, node, actor_thread: str) -> dict:
    """Activate pending work through the canonical selection owner."""
    result = admission.activate(
        node.id, expected_revision=node.revision, actor_id=actor_thread,
        provenance=_PROVENANCE, db=db,
    )
    db.commit()
    return result


def _register(db, parent_thread: str, external_id: str, *, origin_thread: str, turn_id: str):
    """Register one workspace document under a trusted Turn origin."""
    return models.register_artifact(
        integration="workspace",
        type="document",
        external_id=external_id,
        parent_thread_id=parent_thread,
        title=external_id,
        origin=RegistrationOrigin(origin_thread, turn_id, "artifact_tool"),
        db=db,
    )


def _registration(db, artifact_id: str, thread_id: str):
    return db.execute(
        """SELECT * FROM artifact_registrations
           WHERE artifact_id=? AND parent_thread_id=?""",
        (artifact_id, thread_id),
    ).fetchone()


def _origin_title(db, artifact_id: str, thread_id: str) -> str:
    """Name the registration's origin node so a wrong binding reads plainly."""
    row = db.execute(
        """SELECT n.title FROM artifact_registrations r
           JOIN plan_nodes n ON n.id=r.origin_plan_node_id
           WHERE r.artifact_id=? AND r.parent_thread_id=?""",
        (artifact_id, thread_id),
    ).fetchone()
    return row["title"] if row is not None else "<unbound>"


def _origin_metadata(db, artifact_id: str, thread_id: str) -> dict:
    """Return the origin resolution recorded on the latest registered event."""
    row = db.execute(
        """SELECT metadata FROM artifact_registration_events
           WHERE artifact_id=? AND affected_thread_id=? AND event_type='registered'
           ORDER BY rowid DESC LIMIT 1""",
        (artifact_id, thread_id),
    ).fetchone()
    return json.loads(row["metadata"])


def _turn(db, turn_id: str):
    return db.execute("SELECT * FROM active_turns WHERE id=?", (turn_id,)).fetchone()


def _open_selection_count(db, executor: str) -> int:
    return int(db.execute(
        """SELECT COUNT(*) FROM plan_node_attempts
           WHERE executor_thread_id=? AND released_at IS NULL""",
        (executor,),
    ).fetchone()[0])


@dataclass(frozen=True)
class _Selection:
    plan_id: str
    node: PlanNode
    attempt_id: str


def _bind_turn(db, turn_id: str, selection: _Selection) -> None:
    """Write the Turn's accounting snapshot through the canonical infra writer."""
    bind_turn_to_plan_attempt(
        db, turn_id=turn_id, plan_id=selection.plan_id, node_id=selection.node.id,
        attempt_id=selection.attempt_id, commitment_id=selection.node.commitment_id,
        usage_role="execution",
    )
    db.commit()


@pytest.mark.usefixtures("isolated_db")
def test_same_turn_a_to_b_attributes_new_artifacts_to_the_selected_successor():
    """Settling A selects B in the same Turn; B's artifacts bind to B and outlive A."""
    db = get_db()
    thread_id = make_session(db, prefix="origin-a-to-b")
    turn = open_turn(db, thread_id, "turn-a-to-b")
    plan = service.ensure_plan("thread", thread_id, db=db)
    task_a = add_task(db, plan.id, "Task A")
    task_b = add_task(db, plan.id, "Task B")
    _activate(db, task_a, thread_id)
    # B is live work waiting for the executor slot that A occupies.
    service.transition_node(task_b.id, "active", db=db)
    a_artifact = _register(
        db, thread_id, "task-a.md", origin_thread=thread_id, turn_id=turn.turn_id,
    )
    assert _origin_title(db, a_artifact["id"], thread_id) == "Task A"

    _request_review(db, thread_id, plan.id, task_a.id)
    b_artifact = _register(
        db, thread_id, "task-b.md", origin_thread=thread_id, turn_id=turn.turn_id,
    )

    assert _origin_title(db, b_artifact["id"], thread_id) == "Task B"
    origin = _origin_metadata(db, b_artifact["id"], thread_id)
    assert (origin["origin_outcome"], origin["node_state"], origin["usage_role"]) == (
        "bound", "active", "execution",
    )
    # A stays the Turn's write-once accounting snapshot; it did not decide origin.
    assert _turn(db, turn.turn_id)["plan_node_id"] == task_a.id

    _accept(db, thread_id, plan.id, task_a.id)

    assert _registration(db, a_artifact["id"], thread_id)["retired_at"] is not None
    survived = _registration(db, b_artifact["id"], thread_id)
    assert survived["surfacing"] == "visible"
    assert survived["retired_at"] is None
    late = _register(
        db, thread_id, "task-b-late.md", origin_thread=thread_id, turn_id=turn.turn_id,
    )
    assert _origin_title(db, late["id"], thread_id) == "Task B"

    _request_review(db, thread_id, plan.id, task_b.id)
    _accept(db, thread_id, plan.id, task_b.id)

    for artifact in (b_artifact, late):
        settled = _registration(db, artifact["id"], thread_id)
        assert settled["surfacing"] == "settled"
        assert settled["retired_at"] is not None


@pytest.mark.usefixtures("isolated_db")
def test_origin_uses_the_own_plan_selection_not_a_legacy_attempt_the_turn_names():
    """Only the thread's own plan serves it, whatever its Turn's accounting names.

    The child still holds an older attempt in its parent's plan, as legacy data
    can, and its Turn's accounting snapshot names that task.
    """
    parent_id = mk_session("origin-order-parent")
    child_id = mk_session("origin-order-child", spawned_by=parent_id)
    db = get_db()
    parent_plan = service.ensure_plan("thread", parent_id, db=db)
    legacy = leave_attempt_in_plan(db, parent_plan.id, child_id)
    child_plan = service.ensure_plan("thread", child_id, db=db)
    _activate(db, add_task(db, child_plan.id, "Child-owned"), child_id)
    assert _open_selection_count(db, child_id) == 2
    turn = open_turn(db, child_id, "turn-order")
    _bind_turn(db, turn.turn_id, _Selection(
        parent_plan.id, service.get_node(legacy["node_id"], db=db), legacy["id"],
    ))

    artifact = _register(
        db, child_id, "child-work.md", origin_thread=child_id, turn_id=turn.turn_id,
    )

    assert _origin_title(db, artifact["id"], child_id) == "Child-owned"
    assert _turn(db, turn.turn_id)["plan_node_id"] == legacy["node_id"]


@pytest.mark.usefixtures("isolated_db")
def test_reviewer_verification_turn_without_selection_keeps_the_reviewed_node():
    """With no selection of its own, a reviewer's Turn binding decides origin."""
    parent_id = mk_session("origin-reviewer")
    db = get_db()
    plan = service.ensure_plan("thread", parent_id, db=db)
    node = add_task(db, plan.id, "Reviewed work")
    _activate(db, node, parent_id)
    _request_review(db, parent_id, plan.id, node.id)
    review_turn = open_turn(db, parent_id, "turn-review")
    review_usage.bind(
        node.id, reviewer_thread_id=parent_id, turn_id=review_turn.turn_id, db=db,
    )
    db.commit()
    assert _open_selection_count(db, parent_id) == 0
    assert _turn(db, review_turn.turn_id)["plan_usage_role"] == "verification"

    artifact = _register(
        db, parent_id, "review-notes.md", origin_thread=parent_id,
        turn_id=review_turn.turn_id,
    )

    assert _origin_title(db, artifact["id"], parent_id) == "Reviewed work"
    origin = _origin_metadata(db, artifact["id"], parent_id)
    assert (origin["origin_outcome"], origin["node_state"], origin["usage_role"]) == (
        "bound", "review_requested", "verification",
    )


@pytest.mark.usefixtures("isolated_db")
def test_unbound_turn_without_selection_stays_unbound():
    """Pending plan work is not a selection and implies no origin."""
    db = get_db()
    thread_id = make_session(db, prefix="origin-unbound")
    plan = service.ensure_plan("thread", thread_id, db=db)
    add_task(db, plan.id, "Not activated")
    turn = open_turn(db, thread_id, "turn-unbound")

    artifact = _register(
        db, thread_id, "unbound.md", origin_thread=thread_id, turn_id=turn.turn_id,
    )

    assert _registration(db, artifact["id"], thread_id)["origin_plan_node_id"] is None
    origin = _origin_metadata(db, artifact["id"], thread_id)
    assert (origin["origin_outcome"], origin["origin_reason"]) == ("unbound", "turn_unbound")


def _rejected_identity(db, case: str, thread_id: str, other_id: str) -> tuple[str, str, str]:
    """Return (origin thread, turn id, registering thread) for one rejected identity."""
    if case == "turn_unknown":
        return thread_id, "turn-missing", thread_id
    if case == "turn_thread_mismatch":
        return thread_id, open_turn(db, other_id, "turn-other").turn_id, thread_id
    if case == "turn_not_live":
        finished = open_turn(db, thread_id, "turn-finished")
        finish_turn(db, finished.turn_id)
        return thread_id, finished.turn_id, thread_id
    live = open_turn(db, thread_id, "turn-live")
    return thread_id, live.turn_id, other_id


@pytest.mark.usefixtures("isolated_db")
@pytest.mark.parametrize(
    "case",
    ("turn_unknown", "turn_thread_mismatch", "turn_not_live", "origin_thread_mismatch"),
)
def test_rejected_turn_identity_never_borrows_a_valid_selection(case):
    """Identity checks and their reasons run before any selection lookup."""
    db = get_db()
    thread_id = make_session(db, prefix="origin-identity")
    other_id = make_session(db, prefix="origin-other")
    plan = service.ensure_plan("thread", thread_id, db=db)
    _activate(db, add_task(db, plan.id, "Selected work"), thread_id)
    assert _open_selection_count(db, thread_id) == 1
    origin_thread, turn_id, parent_thread = _rejected_identity(db, case, thread_id, other_id)

    artifact = _register(
        db, parent_thread, f"{case}.md", origin_thread=origin_thread, turn_id=turn_id,
    )

    assert _registration(db, artifact["id"], parent_thread)["origin_plan_node_id"] is None
    origin = _origin_metadata(db, artifact["id"], parent_thread)
    assert (origin["origin_outcome"], origin["origin_reason"]) == ("unbound", case)


@pytest.mark.usefixtures("isolated_db")
@pytest.mark.parametrize(
    ("operation", "reason"),
    (("fail", "node_terminal"), ("archive_node", "node_archived")),
)
def test_turn_bound_to_settled_work_without_selection_stays_unbound(operation, reason):
    """The Turn-binding fallback never binds a failed or archived node."""
    db = get_db()
    thread_id = make_session(db, prefix=f"origin-{operation}")
    turn = open_turn(db, thread_id, f"turn-{operation}")
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = add_task(db, plan.id, "Settled work")
    activated = _activate(db, node, thread_id)
    _mutate(db, thread_id, plan.id, operation, {
        "node_id": node.id, "expected_revision": activated["node"]["revision"],
    })
    assert _open_selection_count(db, thread_id) == 0
    assert _turn(db, turn.turn_id)["plan_node_id"] == node.id

    artifact = _register(
        db, thread_id, f"{operation}.md", origin_thread=thread_id, turn_id=turn.turn_id,
    )

    assert _registration(db, artifact["id"], thread_id)["origin_plan_node_id"] is None
    origin = _origin_metadata(db, artifact["id"], thread_id)
    assert (origin["origin_outcome"], origin["origin_reason"]) == ("unbound", reason)
