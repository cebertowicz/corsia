"""Matrix sections B and E: conditions inside the worker, and decisions.

One idea runs through every case. A delegated worker's ordinary progress is a
**digest** entry the parent reads at its next Turn boundary; the parent is
**woken** only when it has something to do -- a result to judge, a failure to
resolve, or a worker that stopped.

Section A (the worker's step lifecycle) is in
``test_child_parent_notice_lifecycle.py``; both share one lineage fixture.
"""

from __future__ import annotations

import pytest

from plans import holds, lifecycle, mutations, service
from plans.boundary import PlanActor
from tests.child_parent_notice_helpers import (
    DEADLINE,
    begin_worker_turn,
    db,
    delegated,
    end_worker_turn,
    notice_kinds,
    notices,
    transcript,
    wakes,
    worker_actor,
)
from tests.plan_review_helpers import submit_review_evidence
from tests.plan_scene import _artifact

__all__ = ["db"]


# --- B. Execution conditions inside the child ---------------------------------


def test_b1_child_sequencing_digests_instead_of_waking_the_parent(db):
    """B1: ordering the worker's own steps is never a block (rule 3)."""
    _step, plan, node = delegated(db)
    prerequisite = service.add_node(
        plan.id, kind="task", title="Prerequisite",
        acceptance_criteria="Prerequisite passes", db=db,
    )

    service.add_dependency(node.id, prerequisite.id, actor_id="worker", db=db)

    assert wakes(db) == []
    assert notice_kinds(db) == ["plan.child_dependency_added"]


def test_who_reviews_is_read_from_the_reviewer_not_from_the_event_name(db):
    """A3 drops node reviews from wake routing without touching decisions.

    The two look alike and are not: a *node*'s reviewer is always the owner of
    the plan it lives in, so a worker's review is its own and the parent has
    nothing to do. A *decision*'s reviewer is the parent tier, so the parent
    does. Both are read from who reviews, never from the event's name, which
    is why dropping ``review_requested`` from wake routing cannot silently
    take the parent out of its own decision review.
    """
    from plans import boundary, decision_authz

    _step, plan, node = delegated(db)
    plan_row = service._plan_row(db, plan.id)

    author, reviewer = decision_authz.tiers(db, plan_row)
    assert (author, reviewer) == ("worker", "parent")

    # The same node, reviewed by the worker that owns the plan: permitted.
    boundary._authorize_reviewer(
        plan_row, service._node_row(db, node.id), worker_actor(),
    )
    # And the parent is not a reviewer of the worker's own node.
    with pytest.raises(PermissionError):
        boundary._authorize_reviewer(
            plan_row, service._node_row(db, node.id),
            PlanActor("parent", "user-1", "project-1"),
        )


def test_b3_failed_prerequisite_reaches_the_parent_as_the_failure(db):
    """B3: the wake is the failure itself, not the sequencing behind it."""
    _step, plan, node = delegated(db)
    prerequisite = service.add_node(
        plan.id, kind="task", title="Prerequisite",
        acceptance_criteria="Prerequisite passes", db=db,
    )
    service.add_dependency(node.id, prerequisite.id, actor_id="worker", db=db)
    begin_worker_turn(db)

    mutations.fail_node(prerequisite.id, actor_id="worker", db=db)
    end_worker_turn(db)

    assert [row["id"] for row in wakes(db)] == [
        "plan-wake:child_step_failed:turn-1:0",
    ]


def test_b5_cleared_child_blocker_digests(db):
    """B5: a blocker cleared is progress, not a wake.

    A blocker hold is only ever the user's; one on anyone else is a wait.
    """
    _step, _plan, node = delegated(db)
    blocked = holds.block(
        node.id, reason_key="missing_input", reason="Needs the user's fixture",
        owner_kind="user", owner_id=None,
        expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="worker", provenance="test", db=db,
    )
    hold_id = blocked["admission"]["causes"][0]["id"]
    wakes_after_block = len(wakes(db))

    holds.clear(
        node.id, hold_id, expected_kind="blocked",
        expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="worker", provenance="test", db=db,
    )

    assert len(wakes(db)) == wakes_after_block
    assert notice_kinds(db)[-1] == "plan.child_blocker_cleared"


def test_b6_armed_child_watch_digests_the_event_and_deadline(db):
    """B6: the parent learns what the worker is waiting for and until when."""
    from plans import watch_operations

    _step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)

    watch_operations.arm(
        plan.id,
        {
            "node_id": node.id,
            "condition": {"type": "deadline", "value": DEADLINE},
            "expected_event": "ci.completed",
            "expected_revision": service.get_node(node.id, db=db).revision,
        },
        worker_actor(),
        db,
    )

    assert wakes(db) == []
    row = notices(db)[-1]
    assert row["change_kind"] == "plan.child_watch_armed"
    assert "ci.completed" in str(row["summary"])
    assert DEADLINE in str(row["summary"])


def test_b6_event_condition_never_renders_a_spurious_until(db):
    """Bug fix: only a deadline renders 'until <value>' (rule 7).

    An event condition's own value is the awaited event's own name, so
    appending it read as "awaiting reviewer.verdict until reviewer.verdict".
    Every event wait now has a deadline (W4), and that is what it waits until.
    """
    from plans import watch_operations

    _step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)

    watch_operations.arm(
        plan.id,
        {
            "node_id": node.id,
            "condition": {"type": "event", "value": "reviewer.verdict"},
            "artifact_id": _artifact(db, external_id="owner/repo#1", owner="worker"),
            "expected_event": "reviewer.verdict",
            "expected_revision": service.get_node(node.id, db=db).revision,
        },
        worker_actor(),
        db,
    )

    assert wakes(db) == []
    row = notices(db)[-1]
    assert row["change_kind"] == "plan.child_watch_armed"
    summary = str(row["summary"])
    assert "awaiting reviewer.verdict until " in summary
    assert "until reviewer.verdict" not in summary


def test_b7_resolved_child_watch_digests(db):
    """B7: the wait ending is progress the parent reads at its own boundary."""
    from plans import watch_operations

    _step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    armed = watch_operations.arm(
        plan.id,
        {
            "node_id": node.id,
            "condition": {"type": "deadline", "value": DEADLINE},
            "expected_event": "ci.completed",
            "expected_revision": service.get_node(node.id, db=db).revision,
        },
        worker_actor(),
        db,
    )

    watch_operations.resolve(
        plan.id,
        {
            "node_id": node.id,
            "watch_id": armed["watch"]["id"],
            "expected_revision": service.get_node(node.id, db=db).revision,
        },
        worker_actor(),
        db,
    )

    assert wakes(db) == []
    assert notice_kinds(db)[-1] == "plan.child_watch_resolved"


def test_b7_a_wait_ended_by_its_event_digests_on_that_path_too(db):
    """B7 holds for the path that actually ends most waits: the event arrived.

    The plan tool is not the only way a wait ends -- usually the awaited event
    reaches the worker and resolves the watch there. The parent hears the same
    thing either way, or the digest would depend on which path ran.
    """
    from plans import watch_operations, watch_resolution

    _step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    armed = watch_operations.arm(
        plan.id,
        {
            "node_id": node.id,
            "condition": {"type": "deadline", "value": DEADLINE},
            "expected_event": "ci.completed",
            "expected_revision": service.get_node(node.id, db=db).revision,
        },
        worker_actor(),
        db,
    )
    db.execute(
        """UPDATE expected_event_watches
           SET artifact_integration='github',artifact_type='pull_request',
               artifact_external_id='42' WHERE id=?""",
        (armed["watch"]["id"],),
    )

    class _Identity:
        integration, type, external_id = "github", "pull_request", "42"

    resolved = watch_resolution.resolve_delivered_event(
        _Identity(), "ci.completed", recipient="worker", db=db,
    )

    assert resolved == [armed["watch"]["id"]]
    assert wakes(db) == []
    assert notice_kinds(db)[-1] == "plan.child_watch_resolved"


def test_b8_expired_child_watch_digests_and_never_wakes_the_parent(db):
    """B8: expiry is the worker's to investigate; the parent only hears of it."""
    _step, _plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)

    service.record_runner_event(
        node.id, "watch_expired", provenance="continuation.t2",
        payload={"trigger_id": "trigger-1", "outcome": "delivered",
                 "reason": "watch_expired"},
        db=db,
    )

    assert wakes(db) == []
    assert notice_kinds(db)[-1] == "plan.child_watch_expired"
    assert wakes(db) == []


def test_b9_child_pausing_its_own_step_digests_with_the_reason(db):
    """B9: the worker setting its own step aside is progress with a why."""
    _step, _plan, node = delegated(db)

    holds.pause(
        node.id, "Waiting on the design answer",
        expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="worker", provenance="test", db=db,
    )

    assert wakes(db) == []
    row = notices(db)[-1]
    assert row["change_kind"] == "plan.child_node_paused"
    assert "Waiting on the design answer" in str(row["summary"])


def test_b10_user_pause_of_a_child_step_is_labeled_and_never_nudges(db):
    """B10: a pause the user set never asks any agent to act (rule 6)."""
    _step, _plan, node = delegated(db)

    holds.pause(
        node.id, "Parked for the release",
        expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="user:user-1", provenance="api.plans", db=db,
    )

    assert wakes(db) == []
    row = notices(db)[-1]
    assert row["change_kind"] == "plan.child_node_paused"
    assert "paused by user" in str(row["summary"]).lower()


def test_b10_user_pause_of_the_child_plan_is_labeled_and_never_nudges(db):
    """B10: the whole-plan case reads the same way, from the same rule."""
    _step, plan, _node = delegated(db)

    lifecycle.transition(
        plan.id, "paused",
        expected_revision=service._plan_row(db, plan.id)["revision"],
        actor_id="user:user-1", provenance="api.plans", db=db,
    )

    assert wakes(db) == []
    row = notices(db)[-1]
    assert row["change_kind"] == "plan.child_plan_paused"
    assert "paused by user" in str(row["summary"]).lower()


def test_b11_child_pausing_its_whole_plan_wakes_the_parent(db):
    """B11: the worker stopping itself is something the parent must look into."""
    _step, plan, _node = delegated(db)

    lifecycle.transition(
        plan.id, "paused",
        expected_revision=service._plan_row(db, plan.id)["revision"],
        actor_id="worker", provenance="tool.plans", db=db,
    )

    appended = wakes(db)
    assert len(appended) == 1
    content = str(appended[0]["content"]).lower()
    assert "paused on the user's word" in content
    assert "only the user resumes" in content


def _set_plan_state(db, plan_id: str, state: str, actor: str | None) -> None:
    lifecycle.transition(
        plan_id, state,
        expected_revision=service._plan_row(db, plan_id)["revision"],
        actor_id=actor, provenance="tool.plans", db=db,
    )


def test_b11_a_later_self_pause_wakes_the_parent_again(db):
    """B11: each time the worker stops itself is news; the first wake mutes nothing."""
    _step, plan, _node = delegated(db)

    _set_plan_state(db, plan.id, "paused", "worker")
    _set_plan_state(db, plan.id, "active", "worker")
    _set_plan_state(db, plan.id, "paused", "worker")

    assert len(wakes(db)) == 2


def test_b11_a_pause_the_worker_did_not_set_never_claims_it_did(db):
    """B11: the parent's own pause, or one with no actor, is a digest, not a false wake."""
    _step, plan, _node = delegated(db)

    _set_plan_state(db, plan.id, "paused", "parent")
    _set_plan_state(db, plan.id, "active", "parent")
    _set_plan_state(db, plan.id, "paused", None)

    assert wakes(db) == []
    paused = [r for r in notices(db) if r["change_kind"] == "plan.child_plan_paused"]
    assert len(paused) == 2
    assert all("by user" not in str(r["summary"]).lower() for r in paused)


def _wait_on(db, plan, node, *, reason, owner_kind="external", actor=None):
    """Block a step on someone other than the user: ``block_node``'s own write.

    A blocker anyone but the user clears is a wait (``plans.blocked_wait``),
    armed through the same writer ``block_node`` uses.
    """
    from plans import blocked_wait, watch_operations

    params = blocked_wait.watch_params({
        "node_id": node.id, "reason_key": "missing_input", "reason": reason,
        "owner_kind": owner_kind,
        "expected_revision": service.get_node(node.id, db=db).revision,
    })
    return watch_operations.arm_write(plan.id, params, actor or worker_actor(), db)


def test_b4_child_blocking_itself_wakes_the_parent(db):
    """B4: a worker's blocker on anyone but the user is the parent's to resolve.

    It is a wait, not a hold, and its arming wakes the parent once, naming
    the reason and what the parent can do.
    """
    _step, plan, node = delegated(db)

    _wait_on(db, plan, node, reason="Needs the upstream fixture")

    appended = wakes(db)
    assert len(appended) == 1
    content = str(appended[0]["content"])
    assert "Needs the upstream fixture" in content
    lowered = content.lower()
    assert "resolve" in lowered and "decision" in lowered


def test_b4_a_blocker_the_parent_placed_is_its_own_receipt(db):
    """The parent's own block on the child's node never wakes it (rule: an
    actor's own action is a receipt, matching every other ``_wake_parent`` case)."""
    _step, plan, node = delegated(db)

    _wait_on(
        db, plan, node, reason="Parent stepped in directly",
        actor=PlanActor("parent", "user-1", "project-1"),
    )

    assert wakes(db) == []


def test_b4_each_new_self_blocker_wakes_again(db):
    """A wait that ended and was raised again is new news, like B11's repeat self-pause."""
    from plans import watch_operations

    _step, plan, node = delegated(db)

    first = _wait_on(db, plan, node, reason="First blocker")
    watch_operations.resolve(
        plan.id,
        {"node_id": node.id, "watch_id": first.id,
         "expected_revision": service.get_node(node.id, db=db).revision},
        worker_actor(), db,
    )
    _wait_on(db, plan, node, reason="Second blocker")

    assert len(wakes(db)) == 2


def test_b4_a_blocker_on_anyone_but_the_user_is_never_a_hold(db):
    """Only the user's own blocker is a hold; the hold writer refuses the rest."""
    _step, _plan, node = delegated(db)

    with pytest.raises(service.PlanInputError, match="wait"):
        holds.block(
            node.id, reason_key="missing_input", reason="Needs the fixture",
            owner_kind="external", owner_id=None,
            expected_revision=service.get_node(node.id, db=db).revision,
            actor_id="worker", provenance="test", db=db,
        )


def test_b4_a_blocker_the_user_placed_is_a_digest_not_a_wake(db):
    """PR #2008 review: only the worker's own blocker is the parent's to clear."""
    _step, _plan, node = delegated(db)

    holds.block(
        node.id, reason_key="missing_input", reason="Waiting on my sign-off",
        owner_kind="user", owner_id=None,
        expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="user:user-1", provenance="test", db=db,
    )

    assert wakes(db) == []
    assert notice_kinds(db) == ["plan.child_blocker_raised"]
    summary = str(notices(db)[0]["summary"])
    assert "by user" in summary and "Waiting on my sign-off" in summary


def test_b4_a_blocker_the_user_owns_is_a_digest_not_a_wake(db):
    """The worker placed it, but the user is the one who can clear it."""
    _step, _plan, node = delegated(db)

    holds.block(
        node.id, reason_key="needs_approval", reason="Needs the user's approval",
        owner_kind="user", owner_id=None,
        expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="worker", provenance="test", db=db,
    )

    assert wakes(db) == []
    assert notice_kinds(db) == ["plan.child_blocker_raised"]
    assert "on the user" in str(notices(db)[0]["summary"])


def test_b4_a_blocker_on_the_system_still_wakes_the_parent(db):
    """PR #2011 review: only the user's own blockers are demoted to a digest."""
    _step, plan, node = delegated(db)

    _wait_on(db, plan, node, reason="Retries exhausted", owner_kind="system")

    assert len(wakes(db)) == 1


# --- E. Decisions and user input ---------------------------------------------


def _drafted(db, node, plan):
    from plans import decisions

    return decisions.draft(
        plan.id, source_node_id=node.id, mutation_id="decide-1",
        actor=worker_actor(), provenance="tool.plans", db=db,
        title="Pick the storage shape",
        context="Two shapes fit the read pattern.",
        tradeoff="Rows are simpler; columns are faster.",
        recommendation="A", rationale="Simpler wins here.",
        options=[
            {"id": "A", "label": "Rows", "detail": "Simple to migrate"},
            {"id": "B", "label": "Columns"},
        ],
        other_enabled=True,
    )


def test_e1_child_drafted_decision_wakes_the_parent_with_full_draft(db):
    """E1: the parent is woken once, labeled as a decision, carrying the full
    draft -- title, context, options (labels and details), recommendation and
    tradeoff -- so it can act without fetching (matrix row E1, settled call 2).

    Matrix fact: on current dev ``decision_drafted`` routes nowhere, so this
    fails first.
    """
    _step, plan, node = delegated(db)

    _drafted(db, node, plan)

    appended = wakes(db)
    assert len(appended) == 1
    content = str(appended[0]["content"])
    assert "Pick the storage shape" in content
    assert "Two shapes fit the read pattern." in content
    assert "Rows are simpler; columns are faster." in content
    assert "Rows" in content and "Simple to migrate" in content
    lowered = content.lower()
    assert "parent_review" in lowered or "raise" in lowered


def test_e1_the_gate_still_only_lets_the_parent_tier_act(db):
    """E1 must not weaken the existing authz gate: only the parent tier may
    ``parent_review``/``raise`` the drafted decision."""
    from plans import decisions

    _step, plan, node = delegated(db)
    drafted = _drafted(db, node, plan)
    decision_id = drafted.result["decision"]["id"]

    with pytest.raises(PermissionError):
        decisions.parent_review(
            decision_id, expected_revision=drafted.result["decision"]["revision"],
            mutation_id="review-worker", actor=worker_actor(),
            provenance="tool.plans", db=db,
        )


def test_e1_the_child_gets_a_digest_not_a_wake_and_is_not_stalled(db):
    """The child hears its decision is with its parent and moves on -- a
    digest in its own transcript, never a second wake of itself."""
    _step, plan, node = delegated(db)

    _drafted(db, node, plan)

    assert wakes(db, thread_id="worker") == []
    own = transcript(db, thread_id="worker")
    assert own, "child should have a passive digest about its own drafted decision"
    lowered = str(own[-1]["content"]).lower()
    assert "parent" in lowered
    assert "next task" in lowered or "pick up" in lowered


def test_e2_answered_child_decision_digests_as_an_answered_decision(db):
    """E2: the child is woken with the answer; the parent only gets a digest.

    Drafting already woke the parent once (E1); answering must not add a
    second wake on top of it.
    """
    from plans import decisions

    _step, plan, node = delegated(db)
    drafted = decisions.draft(
        plan.id, source_node_id=node.id, mutation_id="decide-1",
        actor=worker_actor(), provenance="tool.plans", db=db,
        title="Pick the storage shape",
        context="Two shapes fit the read pattern.",
        tradeoff="Rows are simpler; columns are faster.",
        recommendation="A", rationale="Simpler wins here.",
        options=[{"id": "A", "label": "Rows"}, {"id": "B", "label": "Columns"}],
        other_enabled=True,
    )
    decision_id = drafted.result["decision"]["id"]
    reviewed = decisions.parent_review(
        decision_id, expected_revision=drafted.result["decision"]["revision"],
        mutation_id="review-1", actor=PlanActor("parent", "user-1", "project-1"),
        provenance="tool.plans", db=db,
    )
    raised = decisions.raise_to_user(
        decision_id, expected_revision=reviewed.result["decision"]["revision"],
        mutation_id="raise-1", actor=PlanActor("parent", "user-1", "project-1"),
        provenance="tool.plans", db=db,
    )
    wakes_after_draft = len(wakes(db))

    decisions.answer(
        decision_id, option_id="A",
        expected_revision=raised.result["decision"]["revision"],
        mutation_id="answer-1",
        actor=PlanActor(None, "user-1", "project-1", kind="user"),
        provenance="api.plans", db=db,
    )

    assert len(wakes(db)) == wakes_after_draft
    assert "plan.child_decision_answered" in notice_kinds(db)


# --- C. Friction on one child step --------------------------------------------


def _reject(db, node, feedback="Not yet", actor="worker"):
    """Submit review evidence for this node, then reject it; return its revision."""
    from plans import rejection

    current = service.get_node(node.id, db=db)
    if current.state == "pending":
        service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(
        node.id, db=db, actor_id="worker", evidence=f"{node.id} is done",
    )
    _rejected, revision, _replayed = rejection.reject_submission(
        node.id, feedback=feedback, actor_id=actor,
        provenance="test.friction", db=db,
    )
    return revision


def _friction_notices(db) -> list:
    """Return just the friction digest rows, oldest first."""
    return [row for row in notices(db) if row["change_kind"] == "plan.child_node_friction"]


def test_c1_friction_attempt_2_digests_without_waking_the_parent(db):
    """C1: one rejection is a normal retry the parent only needs to see.

    The rejected submission's own terminal ``failed`` transition is a
    separate, already-covered signal (matrix A6/A7, decided at Turn end);
    this test is scoped to the friction channel only.
    """
    _step, _plan, node = delegated(db)

    _reject(db, node)

    assert wakes(db) == []
    friction = _friction_notices(db)
    assert len(friction) == 1
    assert "attempt 2" in str(friction[0]["summary"])


def test_c2_friction_attempt_3_wakes_the_parent_exactly_once(db):
    """C2: reaching FRICTION_WAKE_ATTEMPTS wakes the parent, naming the step,
    the attempt count and the one expected action (rule 7)."""
    _step, _plan, node = delegated(db)

    first = _reject(db, node)
    _reject(db, first)

    friction = _friction_notices(db)
    assert "attempt 3" in str(friction[-1]["summary"])
    appended = wakes(db)
    assert len(appended) == 1
    content = str(appended[0]["content"])
    assert "Work" in content
    assert "attempt 3" in content
    lowered = content.lower()
    assert "re-brief" in lowered and "stop" in lowered
    assert any(word in lowered for word in ("brief", "scope", "model"))


def test_c2_a_rejection_the_parent_made_digests_without_waking_it(db):
    """The parent's own third rejection is its receipt; a later one by the
    worker still wakes it, once."""
    _step, _plan, node = delegated(db)

    first = _reject(db, node)
    second = _reject(db, first, actor="parent")

    # Its own rejection is not digested back to it either (owner's receipt).
    assert len(_friction_notices(db)) == 1
    assert wakes(db) == []

    _reject(db, second)
    assert "attempt 4" in str(_friction_notices(db)[-1]["summary"])
    appended = wakes(db)
    assert len(appended) == 1
    # Debt 4: the wake fires at the step's real attempt (4, since the parent's
    # own attempt-3 rejection was silent), and its text must say so, not the
    # threshold constant (3) baked into the wake identity below.
    assert "attempt 4" in str(appended[0]["content"])


def test_c_the_wake_identity_and_the_ordinal_read_one_lineage(db):
    """The root that names the wake and the depth that counts the attempt
    must describe the same chain, or once-only and "attempt N" drift apart."""
    from plans.operator_node_facts import revision_root, revisions

    _step, plan, node = delegated(db)
    second = _reject(db, _reject(db, node))

    assert revision_root(second.id, db=db) == node.id
    assert revision_root(node.id, db=db) == node.id
    assert revisions([plan.id], db=db)[second.id] == 2


def test_c3_further_attempts_past_the_threshold_digest_only(db):
    """C3: the attempt past the threshold keeps digesting; no second wake.

    Attempt 4 (depth 3) is "a further attempt past the threshold" here;
    send-backs are uncapped, so deeper attempts digest the same way.
    """
    _step, _plan, node = delegated(db)

    first = _reject(db, node)
    second = _reject(db, first)
    _reject(db, second)

    friction = _friction_notices(db)
    assert [
        str(row["summary"]).split(":")[0].strip() for row in friction
    ] == [
        "Delegated step attempt 2",
        "Delegated step attempt 3",
        "Delegated step attempt 4",
    ]
    assert len(wakes(db)) == 1


def test_c_harmless_releases_never_count_as_friction(db):
    """Sequencing and a watch are not friction (matrix C's opening rule)."""
    from plans import watch_operations

    _step, plan, node = delegated(db)
    prerequisite = service.add_node(
        plan.id, kind="task", title="Prerequisite",
        acceptance_criteria="Prerequisite passes", db=db,
    )
    service.add_dependency(node.id, prerequisite.id, actor_id="worker", db=db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    watch_operations.arm(
        plan.id,
        {
            "node_id": node.id,
            "condition": {"type": "deadline", "value": DEADLINE},
            "expected_event": "ci.completed",
            "expected_revision": service.get_node(node.id, db=db).revision,
        },
        worker_actor(),
        db,
    )

    _reject(db, node)

    friction = _friction_notices(db)
    assert len(friction) == 1
    assert "attempt 2" in str(friction[0]["summary"])
    assert wakes(db) == []


def test_c_no_parent_attachment_never_wakes_on_friction(db):
    """The wake needs a live delegation; a plan with no parent stays quiet."""
    plan = service.ensure_plan("thread", "worker", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Solo work",
        acceptance_criteria="Tests pass", db=db,
    )

    first = _reject(db, node)
    second = _reject(db, first)
    _reject(db, second)

    assert notices(db, thread_id="worker") == []
    assert wakes(db, thread_id="worker") == []
