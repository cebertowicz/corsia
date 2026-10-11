"""Representative non-code journey for the reliable-Plans first slice.

The owner and its worker each run one venue task in their own plan, since only
a plan's owner runs its tasks. The journey covers a hold, a requirements edit,
resolve, whole-plan pause and resume, acceptance and the operator projection,
and needs no user repair. A second test follows the same journey's started
calls through `plans.tool_dispatches`, recording each only under a valid
selection.
"""

from __future__ import annotations

from dataclasses import dataclass

from infra.database import get_db
from plans import admission, folded_reads, operator_projection, service, tool_dispatches
from plans.boundary import PlanActor, mutate
from tests.event_routing_helpers import mk_session

HUMAN = PlanActor(None, "user-1", None, kind="user")


@dataclass(frozen=True)
class _Venue:
    """The journey's executors, Turns, plans, plan nodes, and first selections."""

    owner: str
    worker: str
    owner_turn: str
    worker_turn: str
    plan_id: str
    phase_id: str
    compare_id: str
    worker_plan_id: str
    travel_id: str
    compare_attempt: str
    travel_attempt: str


def _mutate(
    db, actor_thread: str, plan_id: str, operation: str, params: dict, key: str, *, actor=None,
):
    return mutate(
        plan_id,
        operation,
        params,
        actor=actor or PlanActor(actor_thread, "user-1", None),
        provenance="test.reliable_plans_venue_journey",
        mutation_id=key,
        db=db,
    ).result


def _turn(db, thread_id: str) -> str:
    turn_id = f"turn-{thread_id}"
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'test/model','running','t','test')""",
        (turn_id, thread_id),
    )
    db.commit()
    return turn_id


def _recorded_node(db, dispatch_id: str) -> str | None:
    """Return the node a started call was recorded under, or None if unrecorded."""
    row = db.execute(
        """SELECT a.node_id FROM plan_tool_dispatches d
           JOIN plan_node_attempts a ON a.id=d.attempt_id WHERE d.id=?""",
        (dispatch_id,),
    ).fetchone()
    return None if row is None else row["node_id"]


def _open_attempt_count(db, plan_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM plan_node_attempts WHERE plan_id=? AND released_at IS NULL",
        (plan_id,),
    ).fetchone()[0]


def _activate(db, actor_thread: str, plan_id: str, node, key: str) -> str:
    """Activate one pending task through the boundary and return its attempt id."""
    return _mutate(
        db, actor_thread, plan_id, "activate",
        {"node_id": node.id, "expected_revision": node.revision}, key,
    )["admission"]["selection"]["attempt_id"]


def _venue_plan(db) -> _Venue:
    """Give the owner and its worker one selected venue task each, in their own plans."""
    owner = mk_session("venue-owner")
    worker = mk_session("venue-worker", spawned_by=owner)
    owner_turn = _turn(db, owner)
    worker_turn = _turn(db, worker)
    plan = service.ensure_plan("thread", owner, db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Recommend a venue",
        acceptance_criteria="The venue recommendation is complete", db=db,
    )
    compare = service.add_node(
        plan.id, kind="task", title="Compare accessible venues",
        acceptance_criteria="Recommend a suitable venue with linked evidence",
        parent_id=phase.id, db=db,
    )
    worker_plan = service.ensure_plan("thread", worker, db=db)
    travel = service.add_node(
        worker_plan.id, kind="task", title="Check independent travel times",
        acceptance_criteria="Travel times are checked independently", db=db,
    )
    return _Venue(
        owner, worker, owner_turn, worker_turn, plan.id, phase.id, compare.id,
        worker_plan.id, travel.id,
        _activate(db, owner, plan.id, compare, "activate-compare"),
        _activate(db, worker, worker_plan.id, travel, "activate-travel"),
    )


def _hold_for_accessibility(db, venue: _Venue) -> dict:
    """Block the owner's task on a changed requirement."""
    held = _mutate(
        db, venue.owner, venue.plan_id, "block_node",
        {
            "node_id": venue.compare_id,
            "reason_key": "requirements_changed",
            "reason": "The venue must now be wheelchair accessible",
            "owner_kind": "thread",
            "owner_id": venue.owner,
            "expected_revision": service.get_node(venue.compare_id, db=db).revision,
        },
        "hold-changed-requirements",
    )
    assert held["admission"]["selection"] is None
    return held


def _edit_and_resolve(db, venue: _Venue, held: dict) -> dict:
    """Restate the requirement, clear the hold, and return the resumed task."""
    edited = _mutate(
        db, venue.owner, venue.plan_id, "edit",
        {
            "node_id": venue.compare_id,
            "acceptance_criteria": (
                "Recommend a wheelchair-accessible venue with linked evidence"
            ),
            "expected_revision": held["node"]["revision"],
        },
        "edit-accessibility-requirement",
    )
    assert edited["node"]["requirements_revision"] == 2
    # A blocker the agent clears itself is a wait with a deadline, ended early.
    assert edited["admission"]["causes"][0]["watch_id"] == held["watch"]["id"]
    return _mutate(
        db, venue.owner, venue.plan_id, "resolve_watch",
        {
            "node_id": venue.compare_id,
            "watch_id": held["watch"]["id"],
            "expected_revision": edited["node"]["revision"],
        },
        "resolve-accessibility-wait",
    )


def _pause_and_resume(db, venue: _Venue) -> None:
    """The user pauses the owner's whole plan, then resumes it; its task is reselected."""
    paused = _mutate(
        db, venue.owner, venue.plan_id, "pause",
        {"expected_revision": service._plan_row(db, venue.plan_id)["revision"]},
        "pause-whole-plan", actor=HUMAN,
    )
    assert paused["plan"]["state"] == "paused"
    assert _open_attempt_count(db, venue.plan_id) == 0
    resumed = _mutate(
        db, venue.owner, venue.plan_id, "resume",
        {"expected_revision": paused["plan"]["revision"]},
        "resume-whole-plan", actor=HUMAN,
    )
    assert resumed["plan"]["state"] == "active"
    assert _open_attempt_count(db, venue.plan_id) == 1


def _review_and_accept(db, actor_thread: str, plan_id: str, node_id: str, key: str):
    node = service.get_node(node_id, db=db)
    reviewed = _mutate(
        db, actor_thread, plan_id, "request_review",
        {
            "node_id": node_id,
            "evidence": f"{key} evidence matches the current requirement",
            "expected_revision": node.revision,
        },
        f"{key}-review",
    )["node"]
    return _mutate(
        db, actor_thread, plan_id, "accept",
        {
            "node_id": node_id,
            "verification_result": "passed",
            "expected_revision": reviewed["revision"],
        },
        f"{key}-accept",
    )["node"]


def test_venue_constraint_wait_resume_and_settlement_need_no_user_repair(isolated_db):
    """The journey's plan behavior needs no user repair."""
    db = get_db()
    venue = _venue_plan(db)

    held = _hold_for_accessibility(db, venue)
    assert service._node_row(db, venue.travel_id)["requirements_revision"] == 1
    resumed_task = _edit_and_resolve(db, venue, held)

    assert resumed_task["admission"]["condition"] == "running"
    assert resumed_task["admission"]["selection"]["requirements_revision"] == 2
    assert resumed_task["admission"]["selection"]["attempt_id"] != venue.compare_attempt
    assert (
        admission.project(venue.travel_id, db=db)["selection"]["attempt_id"]
        == venue.travel_attempt
    )

    _pause_and_resume(db, venue)
    assert _review_and_accept(
        db, venue.worker, venue.worker_plan_id, venue.travel_id, "travel",
    )["state"] == "accepted"
    assert _review_and_accept(
        db, venue.owner, venue.plan_id, venue.compare_id, "recommendation",
    )["state"] == "accepted"

    projection = operator_projection.read_thread(venue.owner, actor=HUMAN, db=db)
    plan = projection["plans"][0]
    assert projection["contract_version"] == 2
    assert plan["current_node_ids"] == []
    assert plan["next_node_ids"] == []
    # The finished phase folds, so nothing is left in play. The pane ships the folded
    # phase row; the finished task beneath it is on the phase's folded page.
    [phase] = plan["nodes"]
    assert phase["id"] == venue.phase_id
    assert phase["folded"] is True
    assert phase["branch_progress"] == {"done": 1, "total": 1}
    assert plan["progress"]["done"] == 0
    [compare] = folded_reads.read_folded(
        venue.plan_id, parent=venue.phase_id, actor=HUMAN, db=db,
    )["items"]
    assert compare["id"] == venue.compare_id
    assert compare["display"]["label_key"] == "finished"
    assert compare["has_folded_children"] is False
    assert service._node_row(db, venue.compare_id)["requirements_revision"] == 2
    assert projection["plans"][0]["progress"]["total"] == 0
    worker_plan = operator_projection.read_thread(
        venue.worker, actor=HUMAN, db=db,
    )["plans"][0]
    assert worker_plan["nodes"][0]["display"]["label_key"] == "finished"
    assert (worker_plan["progress"]["done"], worker_plan["progress"]["total"]) == (0, 0)
    db.close()


def test_venue_calls_are_recorded_only_under_a_valid_selection(isolated_db):
    """Recording follows the journey's selections.

    Each executor's started call is recorded under its own selected task; while
    the owner's task is held a call is simply not recorded (nothing refuses
    it); after resume the owner's call is recorded under the reselected task.
    """
    db = get_db()
    venue = _venue_plan(db)

    assert tool_dispatches.record(
        venue.owner, venue.owner_turn, "web_search", dispatch_id="owner-search",
    ) is True
    assert _recorded_node(db, "owner-search") == venue.compare_id
    tool_dispatches.settle("owner-search", "succeeded")

    held = _hold_for_accessibility(db, venue)
    for dispatch_id, tool_name in (
        ("owner-search-while-held", "web_search"),
        ("owner-read-while-held", "plan_read"),
    ):
        assert tool_dispatches.record(
            venue.owner, venue.owner_turn, tool_name, dispatch_id=dispatch_id,
        ) is False
        assert _recorded_node(db, dispatch_id) is None

    assert tool_dispatches.record(
        venue.worker, venue.worker_turn, "web_search", dispatch_id="worker-search",
    ) is True
    assert _recorded_node(db, "worker-search") == venue.travel_id
    tool_dispatches.settle("worker-search", "succeeded")

    _edit_and_resolve(db, venue, held)
    _pause_and_resume(db, venue)

    assert tool_dispatches.record(
        venue.owner, venue.owner_turn, "web_search",
        dispatch_id="owner-search-after-resume",
    ) is True
    assert _recorded_node(db, "owner-search-after-resume") == venue.compare_id
    tool_dispatches.settle("owner-search-after-resume", "succeeded")
    db.close()
