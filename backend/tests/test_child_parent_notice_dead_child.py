"""Matrix section F: what a parent hears when its worker's own thread stops.

Unlike section A, nothing the worker did to its own plan triggers these --
its Turn errored, it stopped with work open and recorded nothing (often a
question for the parent), or its thread is simply gone. Shares the lineage fixture with
``test_child_parent_notice_lifecycle.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
from automation import plan_liveness
from plans import delegation_child_lifecycle, service
from plans.projections import liveness_signal
from tests.child_parent_notice_helpers import begin_worker_turn, db, delegated, wakes
from tests.plan_writer_authorizer import raw_plan_seed

__all__ = ["db"]


def _complete(*args, **kwargs):
    """Call the terminal hook with an explicit post-commit effects collector."""

    return plan_liveness_terminal.on_thread_turn_complete(
        *args,
        effects=PendingTurnEndEffects(),
        **kwargs,
    )


# --- F1. Child Turn errors with open work ------------------------------------


_REFUSED = "You've hit your session limit."


def test_f1_provider_refusal_names_the_model_switch_recovery(db):
    """#2148: a refused worker is recoverable in place, and the wake says how."""
    delegated(db)

    _complete(
        "worker", outcome="error", error=_REFUSED,
        turn_id="turn-1", clears_at="2026-09-19T00:00:00+00:00", db=db,
    )

    appended = wakes(db)
    assert len(appended) == 1
    assert str(appended[0]["id"]).startswith("plan-wake:child_provider_refused:")
    content = str(appended[0]["content"])
    assert _REFUSED in content
    assert "native/model" in content
    assert "delegate_set_model" in content and "delegate_send" in content
    # A clear time the harness only quoted is not a wait anyone armed.
    assert "2026-09-19" not in content
    assert "No reset time is known" in content


def test_f1_refusal_names_the_reset_its_liveness_armed(db):
    """The time is named only when the worker's own reset wait is real."""
    delegated(db)

    delegation_child_lifecycle.notify_turn_error(
        db, "worker", error=_REFUSED, resumes_at="2099-01-01T12:00:00+00:00",
    )

    content = str(wakes(db)[0]["content"])
    assert "resumes on its own at 2099-01-01 12:00 UTC" in content
    assert "No reset time is known" not in content


def test_f1_ordinary_error_never_suggests_a_model_switch(db):
    """A code or process failure is not a provider limit; switching would not help."""
    delegated(db)

    _complete(
        "worker", outcome="error", error="crash", turn_id="turn-1", db=db,
    )

    appended = wakes(db)
    assert str(appended[0]["id"]).startswith("plan-wake:child_turn_errored:")
    content = str(appended[0]["content"])
    assert "crash" in content
    assert "delegate_set_model" not in content
    assert "reset" not in content.lower()


def test_f1_repeated_refusals_with_nothing_changed_are_one_episode(db):
    """Liveness retries a refused worker; its parent hears the episode once."""
    delegated(db)

    for turn in ("turn-1", "turn-1", "turn-2", "turn-3"):
        _complete("worker", outcome="error", error=_REFUSED, turn_id=turn, db=db)

    assert len(wakes(db)) == 1


def test_f1_a_new_cause_or_real_progress_is_a_new_wake(db):
    """Only a repeat of the same cause at the same child state is suppressed."""
    from plans import service

    _, worker_plan, _ = delegated(db)

    _complete("worker", outcome="error", error="crash", turn_id="turn-1", db=db)
    _complete("worker", outcome="error", error=_REFUSED, turn_id="turn-2", db=db)
    service.add_node(
        worker_plan.id, kind="task", title="Next step",
        acceptance_criteria="Done", db=db,
    )
    _complete("worker", outcome="error", error=_REFUSED, turn_id="turn-3", db=db)

    kinds = [str(row["id"]).split(":")[1] for row in wakes(db)]
    assert kinds == [
        "child_turn_errored", "child_provider_refused", "child_provider_refused",
    ]


def test_f1_a_recovery_the_parent_tried_that_failed_is_told(db):
    """PR #2194 review: switching and resuming change no plan revision."""
    from infra.input_admission import accept_thread_input

    delegated(db)
    _complete("worker", outcome="error", error=_REFUSED, turn_id="turn-1", db=db)

    # The parent follows the wake: another model, then a resume message.
    db.execute("UPDATE threads SET model='other/model' WHERE id='worker'")
    _complete("worker", outcome="error", error=_REFUSED, turn_id="turn-2", db=db)
    accept_thread_input(
        "worker", role="user", content="Resume", intent="apply_now",
        message_id="parent-resume", metadata={"from_thread": "parent"}, db=db,
    )
    _complete("worker", outcome="error", error=_REFUSED, turn_id="turn-3", db=db)

    appended = wakes(db)
    assert len(appended) == 3
    assert "other/model" in str(appended[1]["content"])


def test_f1_only_the_parents_latest_input_starts_a_new_episode(db):
    """Input from anyone else, however recent, leaves the episode as it was."""
    from infra.input_admission import accept_thread_input

    delegated(db)
    accept_thread_input(
        "worker", role="user", content="Brief", intent="apply_now",
        message_id="parent-brief", metadata={"from_thread": "parent"}, db=db,
    )
    _complete("worker", outcome="error", error=_REFUSED, turn_id="turn-1", db=db)
    accept_thread_input(
        "worker", role="user", content="Note", intent="apply_now",
        message_id="other-note", metadata={"from_thread": "someone-else"}, db=db,
    )
    _complete("worker", outcome="error", error=_REFUSED, turn_id="turn-2", db=db)
    assert len(wakes(db)) == 1

    accept_thread_input(
        "worker", role="user", content="Resume", intent="apply_now",
        message_id="parent-resume", metadata={"from_thread": "parent"}, db=db,
    )
    seen: list[str] = []
    db.set_trace_callback(seen.append)
    try:
        _complete("worker", outcome="error", error=_REFUSED, turn_id="turn-3", db=db)
    finally:
        db.set_trace_callback(None)
    assert len(wakes(db)) == 2
    # Read under the writer lock: it seeks the input order backwards, never the
    # worker's whole history.
    episode = next(sql for sql in seen if sql.lstrip().startswith("SELECT m.input_order AS last"))
    plan = [str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + episode)]
    assert any(step.startswith("SEARCH m USING INDEX idx_messages_input_order") for step in plan), plan


def test_f1_a_different_error_at_the_same_revision_is_told(db):
    """Repeats of one crash are one episode; a new crash is new information."""
    delegated(db)

    for turn, error in (("turn-1", "crash A"), ("turn-2", "crash A"), ("turn-3", "crash B")):
        _complete("worker", outcome="error", error=error, turn_id=turn, db=db)

    assert [str(row["content"]).split("\n")[0].count("crash B") for row in wakes(db)] == [0, 1]


def test_f1_first_turn_provider_refusal_with_empty_plan_tells_parent(db):
    """#501: a provider refusal reaches a parent even before the child plans."""
    delegated(db, with_worker_node=False)

    _complete(
        "worker", outcome="error", error=_REFUSED, turn_id="turn-1", db=db,
    )

    appended = wakes(db)
    assert len(appended) == 1
    assert str(appended[0]["id"]).startswith("plan-wake:child_provider_refused:")
    assert _REFUSED in str(appended[0]["content"])


def test_f1_first_turn_ordinary_error_with_empty_plan_tells_parent(db):
    """#501: an ordinary provider error also beats A11 before child planning."""
    delegated(db, with_worker_node=False)

    _complete(
        "worker", outcome="error", error="boom", turn_id="turn-1", db=db,
    )

    appended = wakes(db)
    assert len(appended) == 1
    assert str(appended[0]["id"]).startswith("plan-wake:child_turn_errored:")
    assert "boom" in str(appended[0]["content"])


def test_f1_repeated_first_turn_refusal_with_empty_plan_never_falls_back_to_a11(db):
    """#501: a deduplicated repeat error still must not emit the A11 fallback."""
    delegated(db, with_worker_node=False)

    for turn in ("turn-1", "turn-2"):
        _complete("worker", outcome="error", error=_REFUSED, turn_id=turn, db=db)

    appended = wakes(db)
    assert len(appended) == 1
    assert str(appended[0]["id"]).startswith("plan-wake:child_provider_refused:")


def test_a11_clean_turn_with_empty_plan_still_tells_parent_once(db):
    """A completed Turn with an empty child plan keeps the A11 recovery path."""
    delegated(db, with_worker_node=False)

    _complete("worker", db=db)
    _complete("worker", db=db)

    appended = wakes(db)
    assert len(appended) == 1
    assert str(appended[0]["id"]).startswith("plan-wake:child_returned_nothing:")


def test_f1_and_a11_never_both_fire_for_one_errored_turn(db):
    """The two rows' guards are exclusive: open work takes F1, and only F1."""
    delegated(db)

    _complete(
        "worker", outcome="error", error="quota exhausted", turn_id="turn-1", db=db,
    )

    appended = wakes(db)
    assert len(appended) == 1
    assert "quota exhausted" in str(appended[0]["content"])


# --- F2. Child stops with open work and records nothing ---------------------


_QUESTION = "Option A or B? I'm holding until you say which."


def _worker_says(db, content: str) -> None:
    """Persist the worker's visible reply, as every harness does before its terminal."""
    from infra.queries import insert_message

    insert_message("worker", "assistant", content, {"turn_id": "turn-1"}, db=db)


def test_f2_first_open_stop_wakes_the_parent_with_the_question(db):
    """#2539: the first Turn end with open work reaches the parent, no stall first."""
    delegated(db)
    _worker_says(db, "Looking into it.")
    _worker_says(db, _QUESTION)

    _complete("worker", db=db)

    appended = wakes(db)
    assert [str(row["id"]).split(":")[1] for row in appended] == ["child_stopped_open"]
    content = str(appended[0]["content"])
    assert json.loads(content.splitlines()[-1])["final_message"] == _QUESTION
    assert "delegate_send" in content.splitlines()[0]


def test_f2_is_told_once_per_child_plan_revision(db):
    """Repeated unchanged Turn ends are one wake; a plan change is a new stop."""
    _step, child_plan, _node = delegated(db)
    for _ in range(3):
        _complete("worker", db=db)
    assert len(wakes(db)) == 1

    service.add_node(
        child_plan.id, kind="task", title="Next", acceptance_criteria="d", db=db,
    )
    _complete("worker", db=db)
    assert len(wakes(db)) == 2


def test_f2_a_follow_up_question_with_no_plan_change_wakes_the_parent_again(db):
    """Debt 84: the parent answered by delegate_send, which moves no plan revision.

    The worker's next question is new news, so it is a new wake carrying that
    question, not a duplicate of the first.
    """
    delegated(db)
    _worker_says(db, _QUESTION)
    _complete("worker", db=db)
    follow_up = "B then. Should it also cover the archived rows?"
    _worker_says(db, follow_up)

    _complete("worker", db=db)

    finals = [
        json.loads(str(row["content"]).splitlines()[-1])["final_message"]
        for row in wakes(db)
    ]
    assert finals == [_QUESTION, follow_up]


def test_f2_bounds_a_long_final_message_and_keeps_its_question(db):
    """The wake carries the question, not an unbounded transcript; it ends the reply."""
    delegated(db)
    _worker_says(db, "x" * 5000 + _QUESTION)

    _complete("worker", db=db)

    final = json.loads(str(wakes(db)[0]["content"]).splitlines()[-1])["final_message"]
    assert len(final) <= 2001 and final.startswith("…") and final.endswith(_QUESTION)


def test_f2_never_carries_a_reply_from_an_earlier_turn(db):
    """A Turn that said nothing carries no message, not an older, maybe answered, one."""
    delegated(db)
    _worker_says(db, _QUESTION)
    begin_worker_turn(db, "turn-2", started_at="2999-01-01T00:00:00")

    _complete("worker", db=db)

    final = json.loads(str(wakes(db)[0]["content"]).splitlines()[-1])["final_message"]
    assert final is None


def test_f2_stays_quiet_while_a_provider_reset_is_armed(db):
    """A refused worker waiting on its reset is F1's; a later Turn end is not F2."""
    delegated(db)
    signal = liveness_signal("worker", db=db)
    plan_liveness.write_row(
        db, "worker", signal,
        deadline=datetime(2099, 1, 1, tzinfo=timezone.utc), cycle=0,
        reset_turn_id="turn-refused",
    )

    _complete("worker", db=db)

    assert wakes(db) == []


def test_f2_stays_quiet_when_the_parent_already_reviews_the_step(db):
    """A step up for review is D4's round, never F2's."""
    step, _plan, _node = delegated(db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='review_requested' WHERE id=?", (step.id,))

    _complete("worker", db=db)

    assert not any("child_stopped_open" in str(row["id"]) for row in wakes(db))


def _stopped_open(db) -> list:
    return [row for row in wakes(db) if "child_stopped_open" in str(row["id"])]


def test_f2_stays_quiet_while_the_worker_waits_on_its_own_watch(db):
    """#2637: a worker parked on CI is waiting on the event, not on its parent.

    The production shape: its step waits on a watch, and a stray pending task
    left in its plan still reads as ready work, so liveness is armed.
    """
    from plans import watch_operations
    from tests.child_parent_notice_helpers import DEADLINE, worker_actor

    _step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    watch_operations.arm(
        plan.id,
        {
            "node_id": node.id,
            "condition": {"type": "deadline", "value": DEADLINE},
            "expected_event": "webhook.github.*",
            "expected_revision": service.get_node(node.id, db=db).revision,
        },
        worker_actor(),
        db,
    )
    service.add_node(plan.id, kind="task", title="Stray", acceptance_criteria="d", db=db)
    assert liveness_signal("worker", db=db).count > 0
    _worker_says(db, "PR #1 is waiting on GitHub gate events.")

    _complete("worker", db=db)

    assert _stopped_open(db) == []


def test_f2_stays_quiet_while_the_worker_is_blocked_on_someone_else(db):
    """A worker that recorded who it waits on has told its parent through B-row digests.

    A blocker anyone but the user clears is a wait with a deadline, armed
    through ``block_node``'s own writer (#2885).
    """
    from tests.child_parent_notice_helpers import worker_actor
    from tests.plan_blocked_wait_helpers import wait_on

    _step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    wait_on(
        db, node.id, reason="Waiting on the review lane", owner_kind="thread",
        reason_key="awaiting_review", actor=worker_actor(),
    )
    service.add_node(plan.id, kind="task", title="Stray", acceptance_criteria="d", db=db)
    assert liveness_signal("worker", db=db).count > 0
    _worker_says(db, "Blocked until the review lane answers.")

    _complete("worker", db=db)

    assert _stopped_open(db) == []


def test_f5_a_turn_with_nothing_movable_is_quiet(db):
    """F5: a completed Turn whose only step is set aside leaves nothing to move."""
    from plans import holds

    _step, _plan, node = delegated(db)
    holds.pause(
        node.id, "Set aside", expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="worker", provenance="test", db=db,
    )

    _complete("worker", db=db)

    assert not any("child_stopped_open" in str(row["id"]) for row in wakes(db))


# --- F3. Child thread archived, purged or deleted with open work ------------


def test_f3_wakes_and_releases_the_attachment_when_the_child_is_gone(db):
    """F3: the parent is woken and the wedged attachment is released."""
    delegated_step, _plan, _node = delegated(db)

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="purged")

    appended = wakes(db)
    assert len(appended) == 1
    assert "purged" in str(appended[0]["content"]).lower()
    refreshed = db.execute(
        "SELECT spawned_thread_id FROM plan_nodes WHERE id=?",
        (delegated_step.id,),
    ).fetchone()
    assert refreshed["spawned_thread_id"] is None


def test_f3_closes_the_childs_own_open_leaves(db):
    """The child's own unsettled work is closed, unblocking the parent's accept."""
    _delegated_step, _plan, node = delegated(db)

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="deleted")

    refreshed = db.execute(
        "SELECT state FROM plan_nodes WHERE id=?", (node.id,),
    ).fetchone()
    assert refreshed["state"] == "closed"


def test_f3_is_idempotent_when_the_end_is_reported_twice(db):
    """A purge after a delete (or a replayed sweep) wakes only once."""
    delegated(db)

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="deleted")
    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="purged")

    assert len(wakes(db)) == 1


def test_f3_user_archive_only_tells_the_parent(db):
    """Reversible: nothing is closed or detached, and the text says so."""
    delegated_step, _plan, node = delegated(db)

    delegation_child_lifecycle.on_child_thread_archived(db, "worker", archived_at="t1")

    appended = wakes(db)
    assert len(appended) == 1
    assert "unarchive it to resume" in str(appended[0]["content"]).lower()
    states = {
        row["id"]: row for row in db.execute(
            "SELECT id,state,spawned_thread_id FROM plan_nodes WHERE id IN (?,?)",
            (delegated_step.id, node.id),
        )
    }
    assert states[delegated_step.id]["spawned_thread_id"] == "worker"
    assert states[node.id]["state"] != "closed"


def test_f3_survives_a_dying_child_that_is_itself_a_delegator(db):
    """A mid-tier delegator's death must not raise out through thread archival.

    Its own open leaf still names a live grandchild, and the archive guard
    refuses such a node. Unhandled, that exception escapes into thread
    archive, session delete, and the retention sweep, where one such thread
    would abort the whole run.
    """
    from plans import mutations, service

    _delegated_step, _worker_plan, worker_node = delegated(db)
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES ('grandchild','user-1','project-1','worker','native/model','t','t')"""
    )
    grandchild_plan = service.ensure_plan("thread", "grandchild", db=db)
    service.add_node(
        grandchild_plan.id, kind="task", title="Deep work",
        acceptance_criteria="Done", db=db,
    )
    service.transition_node(worker_node.id, "active", db=db)
    mutations.attach_spawned_thread(worker_node.id, "grandchild", db=db)

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="purged")

    closed = db.execute(
        "SELECT state,spawned_thread_id FROM plan_nodes WHERE id=?", (worker_node.id,),
    ).fetchone()
    assert closed["state"] == "closed"
    assert closed["spawned_thread_id"] is None
    appended = wakes(db)
    assert len(appended) == 1
    # The grandchild still names the gone delegator, so whoever is left must
    # be told it exists and may need stopping -- never that it is adoptable.
    content = str(appended[0]["content"])
    assert "grandchild still exists with no parent" in content
    assert "adopt" not in content


def _delegator_with_grandchild(db):
    """A delegated worker that has itself delegated a step to 'grandchild'."""
    from plans import mutations

    _step, _worker_plan, worker_node = delegated(db)
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES ('grandchild','user-1','project-1','worker','native/model','t','t')"""
    )
    service.ensure_plan("thread", "grandchild", db=db)
    service.transition_node(worker_node.id, "active", db=db)
    mutations.attach_spawned_thread(worker_node.id, "grandchild", db=db)


def test_f3_a_family_deleted_deepest_first_leaves_no_stranded_worker(db):
    """Delete means delete (#2647): each member's own end detaches it first.

    A family delete ends and removes the deepest thread before its delegator,
    so by the time the delegator ends, nothing it delegated to is left to
    name, and its own plan and its worker's are both retired.
    """
    _delegator_with_grandchild(db)
    delegation_child_lifecycle.on_child_thread_ended(db, "grandchild", reason="deleted")
    db.execute("DELETE FROM threads WHERE id='grandchild'")

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="deleted")

    content = str(wakes(db, "parent")[0]["content"])
    assert "grandchild" not in content
    assert "adopt" not in content
    assert _plan_state(db, "worker") == "archived"
    assert _plan_state(db, "grandchild") == "archived"


def test_f3_says_a_promoted_worker_is_now_top_level(db):
    """A worker the delete promoted keeps running as its own agent (#2647)."""
    _delegator_with_grandchild(db)
    db.execute("UPDATE threads SET spawned_by=NULL WHERE id='grandchild'")

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="deleted")

    content = str(wakes(db)[0]["content"])
    assert "grandchild is now a top-level agent" in content
    assert "adopt" not in content


def _plan_state(db, owner: str) -> str:
    return db.execute(
        "SELECT state FROM plans WHERE owner_kind='thread' AND owner_id=?", (owner,),
    ).fetchone()["state"]


def test_f3_archives_the_gone_childs_own_plan(db):
    """Nothing can act on a deleted thread's plan again, so it is retired (#2647)."""
    _step, worker_plan, _node = delegated(db)

    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="deleted")
    delegation_child_lifecycle.on_child_thread_ended(db, "worker", reason="purged")

    assert _plan_state(db, "worker") == "archived"
    transitions = db.execute(
        """SELECT prior_state,payload_json FROM plan_events
           WHERE plan_id=? AND event='state_transition' AND new_state='archived'""",
        (worker_plan.id,),
    ).fetchall()
    assert len(transitions) == 1
    assert transitions[0]["prior_state"] == "active"
    assert json.loads(transitions[0]["payload_json"]) == {"reason": "deleted"}
    assert _plan_state(db, "parent") == "active"


def test_f3_archives_the_plan_of_a_gone_thread_nobody_delegated_to(db):
    """A top-level agent's own plan goes with it too, with no parent to wake."""
    service.ensure_plan("thread", "loner", db=db)

    delegation_child_lifecycle.on_child_thread_ended(db, "loner", reason="deleted")

    assert _plan_state(db, "loner") == "archived"
    assert wakes(db) == []


def test_f3_user_archive_leaves_the_childs_plan_active(db):
    """A user archive can be undone, so the plan stays as it was."""
    delegated(db)

    delegation_child_lifecycle.on_child_thread_archived(db, "worker", archived_at="t1")

    assert _plan_state(db, "worker") == "active"


def test_f3_stays_quiet_with_no_open_delegation(db):
    """A thread ending with nothing delegated to it has no parent to wake."""
    delegation_child_lifecycle.on_child_thread_ended(db, "nobody", reason="deleted")

    assert wakes(db) == []
