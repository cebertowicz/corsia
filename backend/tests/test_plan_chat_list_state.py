"""The chat list reads one state per thread, by the plan header's own rule (#2220).

Paused, blocked on you, needs input, moving and idle come from
``operator_chat_state.chat_state``, which renames the lead
``operator_node_state.leading`` gives the plan header, through one batched,
user-scoped read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from plans import decisions, not_moving, operator_chat_state, operator_node_state, pane_views, service, status
from plans.boundary import PlanActor
from tests.plan_close_next_helpers import HUMAN, plan_update, revision, task
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture
from tests.plan_writer_authorizer import raw_plan_seed


def _thread(db, thread_id: str, *, user_id: str = "user") -> PlanActor:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, ?, 'test/model', 't', 't')",
        (thread_id, user_id),
    )
    db.commit()
    return PlanActor(thread_id, user_id, None)


def _plan(db, thread_id: str):
    return service.ensure_plan("thread", thread_id, db=db)


def _activate(db, plan_id: str, node_id: str, actor: PlanActor) -> None:
    plan_update(db, plan_id, "activate", {
        "node_id": node_id, "expected_revision": revision(db, node_id),
    }, actor=actor)


def _awaiting_user_review(db, plan_id: str) -> None:
    user_task = task(db, plan_id, "User sign-off", assignee="user")
    _activate(db, plan_id, user_task.id, HUMAN)
    plan_update(db, plan_id, "request_review", {
        "node_id": user_task.id, "evidence": "Ready for you",
        "expected_revision": revision(db, user_task.id),
    }, actor=HUMAN)


def _states(db, ids: list[str]) -> dict:
    return operator_chat_state.read_thread_states(ids, actor=HUMAN, db=db)["states"]


def _awaiting_access_request(db, thread_id: str, *, purpose: str = "Read diagnostics") -> None:
    from infra.host_access_leases import record_application_review, request_lease

    lease = request_lease(thread_id, purpose=purpose, plan=["Read logs"], duration_seconds=60, db=db)
    record_application_review(
        lease["id"], expected_revision=lease["revision"], application_hash=lease["application_hash"],
        allowed=True, reviewer_model="native/reviewer", review_result={"attestation": "Approved"}, db=db,
    )


def test_each_thread_reads_its_one_state(db):
    moving = _thread(db, "moving")
    plan = _plan(db, "moving")
    _activate(db, plan.id, task(db, plan.id, "Build it").id, moving)

    _thread(db, "paused")
    plan = _plan(db, "paused")
    task(db, plan.id, "Later")
    current = service._plan_row(db, plan.id)["revision"]
    plan_update(db, plan.id, "pause", {"expected_revision": current}, actor=HUMAN)

    _thread(db, "blocked")
    _awaiting_user_review(db, _plan(db, "blocked").id)

    needs = _thread(db, "needs")
    plan = _plan(db, "needs")
    _awaiting_user_review(db, plan.id)
    _activate(db, plan.id, task(db, plan.id, "Meanwhile").id, needs)

    _thread(db, "ready")
    plan = _plan(db, "ready")
    task(db, plan.id, "Queued")

    _thread(db, "empty")
    _plan(db, "empty")
    _thread(db, "no-plan")

    states = _states(db, ["moving", "paused", "blocked", "needs", "ready", "empty", "no-plan"])

    assert {key: value["state"] for key, value in states.items()} == {
        "moving": "moving",
        "paused": "paused",
        "blocked": "blocked_on_you",
        "needs": "needs_input",
        "ready": "idle",
        "empty": "idle",
    }
    # The detail is the header's own label, so the tooltip and header agree.
    assert states["moving"]["label_key"] == "working"
    assert states["blocked"]["label_key"] == "needs_you"
    # Queued work nobody moves is idle and reads Waiting, never Working.
    assert states["ready"]["label_key"] == "waiting"
    assert states["paused"]["label_key"] == "waiting"
    assert [key for key, value in states.items() if value["paused"]] == ["paused"]


def test_a_child_review_counts_once_not_again_as_its_parent_rollup(db):
    owner = _thread(db, "child-review")
    plan = _plan(db, "child-review")
    parent = service.add_node(
        plan.id, kind="phase", title="Review phase", acceptance_criteria="Reviewed", db=db,
    )
    child = task(db, plan.id, "Review the document", parent_id=parent.id, assignee="user")
    _activate(db, plan.id, child.id, HUMAN)
    plan_update(db, plan.id, "request_review", {
        "node_id": child.id, "evidence": "Ready for you",
        "expected_revision": revision(db, child.id),
    }, actor=HUMAN)

    state = _states(db, [owner.thread_id])[owner.thread_id]

    assert state["attention_items"] == [{
        "kind": "review", "title": "Review the document", "node_id": child.id,
    }]


def test_a_started_parent_still_counts_its_own_ask(db):
    owner = _thread(db, "parent-review")
    plan = _plan(db, "parent-review")
    parent = service.add_node(
        plan.id, kind="phase", title="Sign off the release", acceptance_criteria="Signed off",
        assignee="user", db=db,
    )
    task(db, plan.id, "Prepare notes", parent_id=parent.id)
    _activate(db, plan.id, parent.id, HUMAN)
    plan_update(db, plan.id, "request_review", {
        "node_id": parent.id, "evidence": "Ready for you",
        "expected_revision": revision(db, parent.id),
    }, actor=HUMAN)

    state = _states(db, [owner.thread_id])[owner.thread_id]

    assert [item["node_id"] for item in state["attention_items"]] == [parent.id]


def test_an_unstarted_parent_counts_its_own_ask_beside_its_child(db):
    from plans import artifact_links
    from registry import attention, models

    owner = _thread(db, "container-ask")
    plan = _plan(db, "container-ask")
    parent = service.add_node(
        plan.id, kind="phase", title="Approve the plan", acceptance_criteria="Approved", db=db,
    )
    child = task(db, plan.id, "Review the document", parent_id=parent.id, assignee="user")
    _activate(db, plan.id, child.id, HUMAN)
    plan_update(db, plan.id, "request_review", {
        "node_id": child.id, "evidence": "Ready for you",
        "expected_revision": revision(db, child.id),
    }, actor=HUMAN)
    # The parent's own raised artifact is a need of its own, not its child's rollup.
    # Only a task holds a review request, so the parent's is non-ask attention.
    models.register_artifact(
        integration="github", type="pr", external_id="container-ask#1",
        parent_thread_id="container-ask", title="Merge the release", db=db,
    )
    raised = db.execute(
        "SELECT id FROM artifact_identities WHERE external_id='container-ask#1'",
    ).fetchone()["id"]
    attention.raise_attention(raised, db=db)
    artifact_links.link(
        parent.id, raised, "attention", actor_id=None, provenance="test", db=db,
    )
    db.commit()

    state = _states(db, [owner.thread_id])[owner.thread_id]

    assert sorted(item["node_id"] for item in state["attention_items"]) == sorted(
        [parent.id, child.id]
    )


def test_paused_plan_defers_its_person_review_from_the_chat_list(db):
    owing = _thread(db, "owing")
    plan = _plan(db, "owing")
    _awaiting_user_review(db, plan.id)
    _activate(db, plan.id, task(db, plan.id, "Meanwhile").id, owing)
    current = service._plan_row(db, plan.id)["revision"]
    plan_update(db, plan.id, "pause", {"expected_revision": current}, actor=HUMAN)

    state = _states(db, ["owing"])["owing"]
    assert state["state"] == "paused"
    assert state["label_key"] == "waiting"
    assert state["paused"] is True
    assert state["attention_items"] == []
    assert status.read(db, service._plan_row(db, plan.id), actor=owing)["severity"] == "not_moving"


def test_another_users_thread_and_an_archived_thread_are_never_read(db):
    foreign = _thread(db, "foreign", user_id="someone-else")
    plan = _plan(db, "foreign")
    _activate(db, plan.id, task(db, plan.id, "Theirs").id, foreign)
    _thread(db, "archived")
    _awaiting_user_review(db, _plan(db, "archived").id)
    db.execute("UPDATE threads SET archived_at='t' WHERE id='archived'")
    db.commit()

    assert _states(db, ["foreign", "archived"]) == {}


def test_a_read_over_the_cap_is_refused_rather_than_truncated(db):
    ids = [f"t{index}" for index in range(operator_chat_state.THREAD_STATE_LIMIT + 1)]

    with pytest.raises(ValueError, match="at most"):
        _states(db, ids)


def test_the_chat_list_never_reads_the_usage_ledger(db):
    """The row returns no cost, so summing the usage ledger per node is wasted:
    on production data it was 44% of the read (2026-09-28)."""
    moving = _thread(db, "moving")
    plan = _plan(db, "moving")
    _activate(db, plan.id, task(db, plan.id, "Build it").id, moving)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        states = _states(db, ["moving"])
    finally:
        db.set_trace_callback(None)

    assert states["moving"]["state"] == "moving"
    assert not [sql for sql in statements if "llm_usage" in sql]


def test_the_chat_list_reads_only_the_facts_its_row_shows(db):
    """The row shows the plan header's word and the ask list, so the attempt
    history, review evidence, last-transition, decision-count and revision-token
    reads the full operator envelope makes are wasted work here."""
    moving = _thread(db, "moving")
    plan = _plan(db, "moving")
    _awaiting_user_review(db, plan.id)
    _activate(db, plan.id, task(db, plan.id, "Build it").id, moving)
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        states = _states(db, ["moving"])
    finally:
        db.set_trace_callback(None)

    assert states["moving"]["state"] == "needs_input"
    assert [item["title"] for item in states["moving"]["attention_items"]] == ["User sign-off"]
    wasted = (
        "PARTITION BY node_id ORDER BY selected_at",  # attempt history
        "review_evidence_submitted",  # latest review evidence
        "MAX(e.created_at) AS created_at",  # last transition per node
        "WITH RECURSIVE tree(id)",  # decision count over the plan subtree
    )
    seen = [" ".join(sql.split()) for sql in statements]
    assert not [marker for marker in wasted if any(marker in sql for sql in seen)]


def test_a_document_ask_reaches_the_chat_row(db):
    held_actor = _thread(db, "held")
    held_plan = _plan(db, "held")
    held = task(db, held_plan.id, "Choose a direction")
    plan_update(db, held_plan.id, "block_node", {
        "node_id": held.id,
        "reason_key": "user_choice",
        "reason": "Choose a direction",
        "owner_kind": "user",
        "owner_id": None,
        "expected_revision": revision(db, held.id),
    }, actor=held_actor)

    state = _states(db, ["held"])["held"]

    assert state["state"] == "blocked_on_you"
    assert state["attention_items"] == [{
        "kind": "hold", "title": "Choose a direction", "node_id": held.id,
    }]


def test_a_human_review_still_reaches_the_row(db):
    _thread(db, "review")
    plan = _plan(db, "review")
    _awaiting_user_review(db, plan.id)

    state = _states(db, ["review"])["review"]

    assert state["state"] == "blocked_on_you"
    assert state["attention_items"][0]["kind"] == "review"
    assert state["attention_items"][0]["title"] == "User sign-off"


def test_a_raised_decision_still_reaches_the_row(db):
    actor = _thread(db, "decision")
    plan = _plan(db, "decision")
    source = task(db, plan.id, "Choose the release shape")
    content = {
        "title": "Choose the release shape",
        "context": "The user owns this product decision.",
        "tradeoff": "Each option changes the user experience.",
        "recommendation": "A",
        "rationale": "The current row preserves context.",
        "options": [
            {"id": "A", "label": "Quiet", "detail": "Keep the current row."},
            {"id": "B", "label": "Loud", "detail": "Add a second destination."},
        ],
        "other_enabled": True,
    }
    decision = decisions.draft(
        plan.id, source_node_id=source.id, mutation_id="chat-row-decision-draft",
        actor=actor, provenance="test.chat-row", db=db, **content,
    ).result["decision"]
    decision = decisions.parent_review(
        decision["id"], expected_revision=decision["revision"],
        mutation_id="chat-row-decision-review", actor=actor, provenance="test.chat-row", db=db,
    ).result["decision"]
    raised = decisions.raise_to_user(
        decision["id"], expected_revision=decision["revision"],
        mutation_id="chat-row-decision-raise", actor=actor, provenance="test.chat-row", db=db,
    ).result["decision"]

    state = _states(db, ["decision"])["decision"]

    assert state["state"] == "blocked_on_you"
    assert state["attention_items"] == [{
        "kind": "decision", "title": "Choose the release shape", "node_id": raised["node_id"],
    }]


def test_access_requests_and_live_failures_reach_the_row(db):
    failed_actor = _thread(db, "failed")
    failed_plan = _plan(db, "failed")
    failed = task(db, failed_plan.id, "Ship the report", assignee="user")
    _activate(db, failed_plan.id, failed.id, failed_actor)
    plan_update(db, failed_plan.id, "fail", {
        "node_id": failed.id, "expected_revision": revision(db, failed.id),
    }, actor=failed_actor)

    _thread(db, "access")
    _plan(db, "access")
    _awaiting_access_request(db, "access")

    states = _states(db, ["failed", "access"])
    assert states["failed"]["state"] == "failed"
    assert states["failed"]["attention_items"] == [{
        "kind": "failed", "title": "Ship the report", "node_id": failed.id,
    }]
    assert states["access"]["state"] == "blocked_on_you"
    assert states["access"]["attention_items"] == [{
        "kind": "access", "title": "Read diagnostics", "node_id": None,
    }]


def test_paused_plan_keeps_an_independent_access_request_visible(db):
    _thread(db, "paused-access")
    plan = _plan(db, "paused-access")
    _awaiting_access_request(db, "paused-access")
    plan_update(db, plan.id, "pause", {
        "expected_revision": service._plan_row(db, plan.id)["revision"],
    }, actor=HUMAN)

    state = _states(db, ["paused-access"])["paused-access"]
    assert state["state"] == "blocked_on_you"
    assert state["paused"] is True
    assert state["attention_items"] == [{
        "kind": "access", "title": "Read diagnostics", "node_id": None,
    }]


def test_access_request_reaches_a_planless_thread_row(db):
    _thread(db, "planless-access")
    _awaiting_access_request(db, "planless-access", purpose="Inspect host")

    state = _states(db, ["planless-access"])["planless-access"]

    assert state == {
        "state": "blocked_on_you",
        "label_key": None,
        "paused": False,
        "attention_items": [{"kind": "access", "title": "Inspect host", "node_id": None}],
        "settled": False,
        "latest_update": None,
        "has_update": False,
        "opened": False,
    }


def test_child_plan_access_request_reaches_its_visible_thread_row(db):
    _thread(db, "requesting-thread")
    child = _thread(db, "child-plan")
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", ("requesting-thread", "child-plan"))
    db.commit()
    child_plan = _plan(db, "child-plan")
    task(db, child_plan.id, "Inspect host")
    _awaiting_access_request(db, child.thread_id, purpose="Inspect child host")

    state = _states(db, ["requesting-thread"])["requesting-thread"]

    assert state == {
        "state": "blocked_on_you",
        "label_key": None,
        "paused": False,
        "attention_items": [{"kind": "access", "title": "Inspect child host", "node_id": None}],
        "settled": False,
        "latest_update": None,
        "has_update": False,
        "opened": False,
    }


def test_access_requests_batch_lineage_queries_by_depth_not_lease_count(db):
    """One batch lookup runs per lineage hop, regardless of unrelated leases."""
    _thread(db, "visible-parent")
    child = _thread(db, "visible-child")
    leaf = _thread(db, "visible-leaf")
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", ("visible-parent", "visible-child"))
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", ("visible-child", "visible-leaf"))
    db.commit()
    _awaiting_access_request(db, leaf.thread_id, purpose="Inspect child")
    for index in range(3):
        thread_id = f"unrequested-{index}"
        _thread(db, thread_id)
        _awaiting_access_request(db, thread_id, purpose="Unrequested lease")

    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        state = _states(db, ["visible-parent"])["visible-parent"]
    finally:
        db.set_trace_callback(None)

    assert state["attention_items"] == [{
        "kind": "access", "title": "Inspect child", "node_id": None,
    }]
    lineage_queries = [
        statement for statement in statements
        if "SELECT id,user_id,project_id,spawned_by FROM threads WHERE id IN" in statement
    ]
    assert len(lineage_queries) <= 3


def test_access_request_is_not_double_attributed_when_parent_and_child_are_requested(db):
    _thread(db, "parent")
    child = _thread(db, "child")
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", ("parent", "child"))
    db.commit()
    _awaiting_access_request(db, child.thread_id, purpose="Inspect child")

    states = _states(db, ["parent", "child"])

    assert states["parent"]["attention_items"] == [{
        "kind": "access", "title": "Inspect child", "node_id": None,
    }]
    assert "child" not in states


def test_purged_descendant_access_request_does_not_block_its_visible_parent(db):
    _thread(db, "parent")
    child = _thread(db, "purged-child")
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", ("parent", "purged-child"))
    db.commit()
    _awaiting_access_request(db, child.thread_id, purpose="Inspect child")
    db.execute("UPDATE threads SET purged_at='t' WHERE id=?", (child.thread_id,))
    db.commit()

    assert _states(db, ["parent"]) == {}


def _failed_task(db, plan_id: str, title: str, actor: PlanActor):
    node = task(db, plan_id, title, assignee="user")
    _activate(db, plan_id, node.id, actor)
    plan_update(db, plan_id, "fail", {
        "node_id": node.id, "expected_revision": revision(db, node.id),
    }, actor=actor)
    return node


def test_actionable_user_asks_outrank_failed_and_settled_failures_never_show(db):
    both_actor = _thread(db, "both")
    both_plan = _plan(db, "both")
    held = task(db, both_plan.id, "Choose a direction")
    plan_update(db, both_plan.id, "block_node", {
        "node_id": held.id, "reason_key": "user_choice", "reason": "Choose a direction",
        "owner_kind": "user", "owner_id": None, "expected_revision": revision(db, held.id),
    }, actor=both_actor)
    _failed_task(db, both_plan.id, "Ship the report", both_actor)

    settled_actor = _thread(db, "settled")
    settled_plan = _plan(db, "settled")
    followed = _failed_task(db, settled_plan.id, "Followed up", settled_actor)
    archived = _failed_task(db, settled_plan.id, "Archived", settled_actor)
    follow_up = task(db, settled_plan.id, "Retry")
    db.execute("UPDATE plan_nodes SET follow_up_of=? WHERE id=?", (followed.id, follow_up.id))
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (archived.id,))

    agent_actor = _thread(db, "agent-hold")
    agent_plan = _plan(db, "agent-hold")
    blocked = task(db, agent_plan.id, "Wait on a peer")
    plan_update(db, agent_plan.id, "block_node", {
        "node_id": blocked.id, "reason_key": "peer", "reason": "Wait on a peer",
        "owner_kind": "thread", "owner_id": None, "expected_revision": revision(db, blocked.id),
    }, actor=agent_actor)
    db.commit()

    states = _states(db, ["both", "settled", "agent-hold"])
    assert states["both"]["state"] == "blocked_on_you"
    assert [item["kind"] for item in states["both"]["attention_items"]] == ["hold"]
    assert states["settled"]["state"] != "failed"
    assert states["settled"]["attention_items"] == []
    assert states["agent-hold"]["state"] not in {"needs_input", "blocked_on_you"}
    assert states["agent-hold"]["attention_items"] == []


def _record_view(db, user_id: str, thread_id: str) -> None:
    pane_views.record(
        db, user_id=user_id, thread_id=thread_id,
        seen_through=datetime.now(UTC).isoformat(timespec="microseconds"),
    )


def test_failed_mark_clears_only_after_the_viewer_sees_it_in_the_plan_pane(db):
    fresh_actor = _thread(db, "fresh-failure")
    fresh_plan = _plan(db, "fresh-failure")
    _failed_task(db, fresh_plan.id, "Never viewed", fresh_actor)

    seen_actor = _thread(db, "seen-failure")
    seen_plan = _plan(db, "seen-failure")
    _failed_task(db, seen_plan.id, "Already viewed", seen_actor)
    _record_view(db, HUMAN.user_id, "seen-failure")

    after_actor = _thread(db, "after-view")
    after_plan = _plan(db, "after-view")
    _record_view(db, HUMAN.user_id, "after-view")
    _failed_task(db, after_plan.id, "Failed after viewing", after_actor)

    other_actor = _thread(db, "other-users-view")
    other_plan = _plan(db, "other-users-view")
    _failed_task(db, other_plan.id, "Other user viewed", other_actor)
    _record_view(db, "someone-else", "other-users-view")

    states = _states(db, ["fresh-failure", "seen-failure", "after-view", "other-users-view"])

    assert states["fresh-failure"]["state"] == "failed"
    assert states["seen-failure"]["state"] == "idle"
    assert states["seen-failure"]["attention_items"] == []
    assert states["after-view"]["state"] == "failed"
    assert states["other-users-view"]["state"] == "failed"



def test_your_move_outranks_an_unseen_failed_mark(db):
    actor = _thread(db, "owed-over-failed")
    plan = _plan(db, "owed-over-failed")
    _failed_task(db, plan.id, "Failed work", actor)
    _awaiting_user_review(db, plan.id)

    state = _states(db, ["owed-over-failed"])["owed-over-failed"]

    assert state["state"] == "blocked_on_you"
    assert [item["kind"] for item in state["attention_items"]] == ["review"]


def test_work_under_a_closed_parent_asks_nothing_of_the_row(db):
    """Close keeps a branch on the plan (#1887), so what sits under it stays out of the row."""
    actor = _thread(db, "closed-branch")
    plan = _plan(db, "closed-branch")
    failed = _failed_task(db, plan.id, "Ship the report", actor)
    held = task(db, plan.id, "Choose a direction")
    plan_update(db, plan.id, "block_node", {
        "node_id": held.id, "reason_key": "user_choice", "reason": "Choose a direction",
        "owner_kind": "user", "owner_id": None, "expected_revision": revision(db, held.id),
    }, actor=actor)
    parent = task(db, plan.id, "Dropped phase")
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (parent.id,))
        db.execute(
            "UPDATE plan_nodes SET parent_id=? WHERE id IN (?,?)", (parent.id, failed.id, held.id),
        )
    db.commit()

    states = _states(db, ["closed-branch"])
    assert states["closed-branch"]["state"] not in {"failed", "blocked_on_you"}
    assert states["closed-branch"]["attention_items"] == []


def test_an_owed_item_is_the_users_move_with_or_without_open_work():
    assert operator_chat_state.chat_state({"state": "active"}, None, owed=True) == "blocked_on_you"
    active = {**dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0), "active": 1}
    assert operator_chat_state.chat_state({"state": "active"}, active, owed=True) == "needs_input"
    assert operator_chat_state.chat_state({"state": "paused"}, active, owed=True) == "blocked_on_you"


def test_pausing_a_plan_hides_its_person_review_until_resumed(db):
    actor = _thread(db, "paused-person-review")
    plan = _plan(db, actor.thread_id)
    _awaiting_user_review(db, plan.id)

    before_pause = _states(db, [actor.thread_id])[actor.thread_id]
    assert before_pause["state"] == "blocked_on_you"
    assert [item["kind"] for item in before_pause["attention_items"]] == ["review"]

    plan_update(db, plan.id, "pause", {
        "expected_revision": service._plan_row(db, plan.id)["revision"],
    }, actor=HUMAN)

    paused = _states(db, [actor.thread_id])[actor.thread_id]
    assert paused["state"] == "paused"
    assert paused["label_key"] == "waiting"
    assert paused["paused"] is True
    assert paused["attention_items"] == []

    plan_update(db, plan.id, "resume", {
        "expected_revision": service._plan_row(db, plan.id)["revision"],
    }, actor=HUMAN)

    resumed = _states(db, [actor.thread_id])[actor.thread_id]
    assert resumed["state"] == "blocked_on_you"
    assert resumed["label_key"] == "needs_you"
    assert [item["kind"] for item in resumed["attention_items"]] == ["review"]


_ZERO = dict.fromkeys(operator_node_state.DISTRIBUTION_KEYS, 0)


@pytest.mark.parametrize(
    ("counts", "state"),
    [
        ({"waiting": 1}, "idle"),
        ({"review_parent_agent": 1}, "moving"),
        ({"blocked": 1}, "idle"),
        ({"blocked": 1, "active": 1}, "moving"),
        ({"review_human": 1, "blocked": 1}, "blocked_on_you"),
        ({"done": 3}, "idle"),
        # The header's own precedence: a block outranks a wait, a ready task
        # beside a wait reads Ready, and all-paused tasks read Paused.
        ({"blocked": 1, "waiting": 1}, "idle"),
        ({"pending_ready": 1, "waiting": 1}, "idle"),
        ({"paused": 2}, "paused"),
    ],
)
def test_a_wait_or_a_block_with_nothing_for_the_user_is_idle(counts, state):
    assert operator_chat_state.chat_state({"state": "active"}, {**_ZERO, **counts}) == state


def _deadline_wait(db, plan_id: str, actor: PlanActor, title: str, ends_at: str) -> None:
    node = task(db, plan_id, title)
    plan_update(db, plan_id, "block_node", {
        "node_id": node.id, "reason_key": "deadline", "reason": "Wait for it",
        "owner_kind": "external", "owner_id": None,
        "condition": {"type": "deadline", "value": ends_at},
        "expected_revision": revision(db, node.id),
    }, actor=actor)


def test_overdue_waits_keep_the_normal_idle_word_without_attention_items(db):
    actor = _thread(db, "three-overdue")
    plan = _plan(db, "three-overdue")
    overdue = (datetime.now(UTC) - not_moving.OVERDUE_GRACE - timedelta(seconds=1)).isoformat()
    for number in range(3):
        _deadline_wait(db, plan.id, actor, f"Wait {number}", overdue)

    state = _states(db, ["three-overdue"])["three-overdue"]

    assert state["state"] == "idle"
    assert state["attention_items"] == []


def test_failed_work_remains_visible_when_pause_defers_a_person_hold(db):
    actor = _thread(db, "stuck-precedence")
    plan = _plan(db, "stuck-precedence")
    overdue = (datetime.now(UTC) - not_moving.OVERDUE_GRACE - timedelta(seconds=1)).isoformat()
    _deadline_wait(db, plan.id, actor, "Overdue", overdue)
    _failed_task(db, plan.id, "Failed first", actor)

    assert _states(db, ["stuck-precedence"])["stuck-precedence"]["state"] == "failed"

    held = task(db, plan.id, "Choose a direction")
    plan_update(db, plan.id, "block_node", {
        "node_id": held.id, "reason_key": "user_choice", "reason": "Choose a direction",
        "owner_kind": "user", "owner_id": None, "expected_revision": revision(db, held.id),
    }, actor=actor)
    assert _states(db, ["stuck-precedence"])["stuck-precedence"]["state"] == "blocked_on_you"

    plan_update(db, plan.id, "pause", {
        "expected_revision": service._plan_row(db, plan.id)["revision"],
    }, actor=HUMAN)
    paused = _states(db, ["stuck-precedence"])["stuck-precedence"]
    assert paused["state"] == "failed"
    assert [item["kind"] for item in paused["attention_items"]] == ["failed"]


def test_a_future_wait_is_idle(db):
    actor = _thread(db, "future-waits")
    plan = _plan(db, "future-waits")
    ends = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    _deadline_wait(db, plan.id, actor, "Wait later", ends)

    state = _states(db, ["future-waits"])["future-waits"]

    assert state["state"] == "idle"
