"""Lifecycle, defensive chronology and shown-state parity through canonical owners."""
from __future__ import annotations

import json

import pytest

from infra.input_admission import accept_thread_input
from infra.send_target import SELECTED_MODEL_UNAVAILABLE
from plans import activity_facts, activity_projection, activity_reducer, admission, holds
from plans import (
    not_moving, operator_display_state, operator_node_state, operator_projection,
    operator_stopped, service, wait_causes,
)
from tests.test_plan_activity_index import db, _actor, _task, _priority_at
from tests.plan_writer_authorizer import raw_plan_seed


def assert_header_parity(db, plan_id):
    owner_id = db.execute("SELECT owner_id FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
    full = operator_projection.read_thread(owner_id, actor=_actor(), db=db)
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    detailed = full["plans"][0]
    row = next(item for item in compact["plans"] if item["plan"]["id"] == plan_id)
    assert row["plan"] == {key: detailed["plan"][key] for key in row["plan"]}
    assert row["owner"] == detailed["owner"]
    assert row["progress"] == {key: detailed["progress"][key] for key in ("open", "liveness")}
    assert row["display"] == {key: detailed["display"][key] for key in ("label_key", "tone_key")}
    assert row["priority_at"] == _priority_at(detailed)
    assert row["has_nodes"] == bool(detailed["nodes"])
    assert compact["attention"] == full["attention"]
    return detailed, row


def assert_project_response_parity(db):
    """Compare every compact response row with its canonical projection peer."""
    full = operator_projection.read_project("p", actor=_actor(), db=db)
    compact = activity_projection.read_project("p", actor=_actor(), db=db)
    full_by_id = {row["plan"]["id"]: row for row in full["plans"]}
    assert compact["target"] == full["target"]
    assert compact["attention"] == full["attention"]
    assert {row["plan"]["id"] for row in compact["plans"]} == set(full_by_id)
    for row in compact["plans"]:
        canonical = full_by_id[row["plan"]["id"]]
        assert row == {
            "plan": {key: canonical["plan"][key] for key in row["plan"]},
            "owner": canonical["owner"],
            "progress": {key: canonical["progress"][key] for key in row["progress"]},
            "has_nodes": bool(canonical["nodes"]),
            "display": {key: canonical["display"][key] for key in row["display"]},
            "priority_at": _priority_at(canonical),
        }
    return compact


def assert_terminal_response_identity(db):
    """The public terminal response is the canonical scope response except time."""
    canonical = activity_projection._scope("project_scope", "p", "Project", _actor(), db=db)
    response = activity_projection.read_project("p", actor=_actor(), db=db)
    assert {key: value for key, value in response.items() if key != "as_of"} == {
        key: value for key, value in canonical.items() if key != "as_of"
    }
    return response


@pytest.mark.parametrize("selection", ["valid", "revision_superseded", "executor_superseded"])
def test_selected_attempt_identity_and_supersession_are_owner_facts(db, selection):
    plan = service.ensure_plan("thread", "moving", db=db)
    task = _task(plan.id, "Selected", db=db)
    service.transition_node(task.id, "active", db=db)
    admission.select(task.id, actor_id="moving", provenance="test", db=db)
    if selection == "revision_superseded":
        db.execute("UPDATE plan_nodes SET requirements_revision=requirements_revision+1 WHERE id=?", (task.id,))
    if selection == "executor_superseded":
        # Attempts are immutable: a changed plan owner supersedes the old executor.
        db.execute("UPDATE plans SET owner_id='blocked' WHERE id=?", (plan.id,))
    detailed, row = assert_header_parity(db, plan.id)
    facts = activity_facts.compact_nodes([plan.id], db=db)[plan.id][0]
    assert (facts["attempt_id"] is not None) == (selection == "valid")
    assert row["progress"]["liveness"] == ("advancing" if selection == "valid" else "idle")
    assert bool(detailed["nodes"][0]["execution"]["selection"]) == (selection == "valid")


@pytest.mark.parametrize("kind,owner", [("paused", "user"), ("paused", "thread"),
    ("blocked", "user"), ("blocked", "thread")])
def test_ordered_placed_hold_attribution_uses_existing_owner(db, kind, owner):
    plan = service.ensure_plan("thread", "moving", db=db)
    task = _task(plan.id, "Held", db=db)
    holds.place_hold(task.id, kind=kind, reason_key="test_hold", reason="Hold",
                     owner_kind=owner, owner_id="moving", expected_revision=task.revision,
                     actor_id="moving", provenance="test", db=db)
    assert_header_parity(db, plan.id)
    facts = activity_facts.compact_nodes([plan.id], db=db)[plan.id][0]
    assert facts["has_user_pause"] == (kind == "paused" and owner == "user")
    assert facts["has_user_block"] == (kind == "blocked" and owner == "user")


@pytest.mark.parametrize("watch_kind", ["plain", "checkin", "blocked", "scheduled"])
def test_watch_cause_classification_and_header_parity(db, watch_kind):
    plan = service.ensure_plan("thread", "moving", db=db)
    task = _task(plan.id, "Wait", db=db)
    db.execute("""INSERT INTO expected_event_watches
        (id,owner_thread_id,plan_id,plan_node_id,condition_type,condition_value,status,armed_at,created_at,updated_at)
        VALUES ('watch','moving',?,?,'deadline','2030-01-01T00:00:00+00:00','armed','t','t','t')""", (plan.id, task.id))
    payload = {"watch_id": "watch"}
    if watch_kind == "blocked":
        payload["blocked"] = {"owner_kind": "external", "owner_id": "peer", "reason": "Wait"}
    if watch_kind == "scheduled":
        payload["scheduled"] = {"reason": "Later"}
    if watch_kind != "plain":
        db.execute("""INSERT INTO plan_node_events(id,node_id,event,provenance,payload_json,created_at)
            VALUES ('armed',?,'watch_armed','test',?,'t')""", (task.id, json.dumps(payload)))
    detailed, row = assert_header_parity(db, plan.id)
    narrow = activity_facts.compact_nodes([plan.id], db=db)[plan.id][0]["causes"][0]
    full = detailed["nodes"][0]["execution"]["causes"][0]
    assert narrow == {key: full[key] for key in narrow}
    assert narrow["blocked_wait"] == (watch_kind == "blocked")
    assert row["progress"]["liveness"] == "waiting"


def test_mixed_offset_deadlines_and_invalid_history_use_parsed_chronology(db):
    plan = service.ensure_plan("thread", "moving", db=db)
    task = _task(plan.id, "Historic deadlines", db=db)
    values = ["2030-01-01T08:00:00+08:00", "2030-01-01T02:00:00Z", "invalid", "2030-01-01T01:00:00"]
    db.executemany("""INSERT INTO expected_event_watches
        (id,owner_thread_id,plan_id,plan_node_id,condition_type,condition_value,status,armed_at,created_at,updated_at)
        VALUES (?,'moving',?,?,'deadline',?,'armed','t','t','t')""",
        [(f"watch-{index}", plan.id, task.id, value) for index, value in enumerate(values)])
    detailed, _row = assert_header_parity(db, plan.id)
    row = activity_facts.compact_nodes([plan.id], db=db)[plan.id][0]
    assert (row["wait_deadline"], row["wait_deadline_latest"]) == (values[0], values[1])
    assert not_moving.deadline_range(detailed["nodes"][0]["execution"]["causes"]) == (values[0], values[1])


@pytest.mark.parametrize("deadline", ["2030-01-01T08:00:00+08:00", "2030-01-01T00:00:00Z"])
def test_current_watch_writer_normalizes_deadlines_before_owner_reads(db, deadline):
    from plans.boundary import PlanActor, mutate

    plan = service.ensure_plan("thread", "moving", db=db)
    task = _task(plan.id, "Schedule", db=db)
    mutate(plan.id, "arm_watch", {
        "node_id": task.id, "expected_revision": task.revision,
        "reason": "Later", "condition": {"type": "deadline", "value": deadline},
    }, actor=PlanActor("moving", "user", "p"), provenance="test", mutation_id="watch-normalized", db=db)
    assert_header_parity(db, plan.id)
    row = activity_facts.compact_nodes([plan.id], db=db)[plan.id][0]
    assert row["wait_deadline"] == row["wait_deadline_latest"] == "2030-01-01T00:00:00+00:00"


def _selected_executor(db, title: str):
    plan = service.ensure_plan("thread", "moving", db=db)
    task = _task(plan.id, title, db=db)
    service.transition_node(task.id, "active", db=db)
    attempt, refusal = admission.select(task.id, actor_id="moving", provenance="test", db=db)
    assert attempt is not None, refusal
    commitment_id = db.execute(
        "SELECT commitment_id FROM plan_nodes WHERE id=?", (task.id,),
    ).fetchone()[0]
    return plan, task, attempt, commitment_id


def _error_turn(db, *, plan, task, attempt, commitment_id, turn_id="turn-error"):
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,'moving',0,'native/model','error','2000-01-01T00:00:00','boot',
                   ?,?,?,?,'execution')""",
        (turn_id, plan.id, task.id, attempt["id"], commitment_id),
    )


def _held_input(db) -> None:
    db.execute("UPDATE threads SET model='native/model' WHERE id='moving'")
    accept_thread_input(
        "moving", role="user", content="Continue", intent="apply_now",
        message_id="held-input", db=db,
    )
    db.execute(
        """UPDATE messages SET write_outcome='proved_no_write', delivery_error=?,
               target_model='native/model', active_turn_id=NULL
           WHERE id='held-input'""",
        (SELECTED_MODEL_UNAVAILABLE,),
    )


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("model", "Stalled: its model is unavailable"),
        ("error", "Stalled: its last Turn failed"),
    ],
)
def test_selected_stalled_executor_has_compact_full_waiting_header_parity(db, kind, reason):
    plan, task, attempt, commitment_id = _selected_executor(db, f"Stalled {kind}")
    _error_turn(db, plan=plan, task=task, attempt=attempt, commitment_id=commitment_id)
    if kind == "model":
        _held_input(db)

    assert wait_causes.stalled_why(db, "moving") == kind
    compact_rows = activity_facts.compact_nodes([plan.id], db=db)
    compact_plan = dict(db.execute("SELECT * FROM plans WHERE id=?", (plan.id,)).fetchone())
    assert activity_facts.executor_stop_facts(compact_rows, {plan.id: compact_plan}, db=db) == {
        task.id: {"stalled": kind},
    }
    detailed, row = assert_header_parity(db, plan.id)

    assert {key: detailed["display"][key] for key in row["display"]} == row["display"] == {
        "label_key": "waiting", "tone_key": "waiting",
    }
    assert detailed["progress"]["distribution"]["waiting"] == 1
    assert detailed["nodes"][0]["display"]["label_key"] == "waiting"
    assert detailed["nodes"][0]["status_reason"] == reason


def test_queued_input_recovers_selected_error_executor_header(db):
    plan, task, attempt, commitment_id = _selected_executor(db, "Queued recovery")
    _error_turn(db, plan=plan, task=task, attempt=attempt, commitment_id=commitment_id)
    _held_input(db)
    db.execute(
        """UPDATE messages SET write_outcome=NULL, delivery_error=NULL, target_model=NULL
           WHERE id='held-input'""",
    )

    detailed, row = assert_header_parity(db, plan.id)

    assert {key: detailed["display"][key] for key in row["display"]} == row["display"] == {
        "label_key": "working", "tone_key": "active",
    }
    assert detailed["progress"]["distribution"]["active"] == 1
    assert detailed["nodes"][0]["status_reason"] is None


def _provider_reset(db, turn_id: str) -> None:
    db.execute(
        """INSERT INTO plan_liveness_wakes
           (id,thread_id,status,deadline,provider_reset_at,provider_reset_turn_id,
            cycle,last_open_count,last_signal_hash,last_settled_events,wakes_in_window,
            armed_at,updated_at)
           VALUES ('reset-wake','moving','armed','2099-01-01T12:00:00+00:00',
                   '2099-01-01T12:00:00+00:00',?,0,0,'',0,0,'t','t')""",
        (turn_id,),
    )


@pytest.mark.parametrize("mismatch", [False, True])
def test_reset_fact_precedence_and_selected_identity_match_compact_full(db, mismatch):
    plan, task, attempt, commitment_id = _selected_executor(db, "Reset precedence")
    _error_turn(db, plan=plan, task=task, attempt=attempt, commitment_id=commitment_id)
    _held_input(db)
    _provider_reset(db, "turn-error")
    if mismatch:
        db.execute("UPDATE active_turns SET plan_attempt_id='superseded' WHERE id='turn-error'")

    detailed, row = assert_header_parity(db, plan.id)

    assert {key: detailed["display"][key] for key in row["display"]} == row["display"]
    if mismatch:
        assert row["display"]["label_key"] == "working"
        assert detailed["nodes"][0]["status_reason"] is None
    else:
        assert row["display"]["label_key"] == "waiting"
        assert detailed["nodes"][0]["status_reason"] == (
            "Provider resets at 2099-01-01T12:00:00+00:00"
        )


def test_queued_input_with_existing_reset_header_parity(db):
    plan, task, attempt, commitment_id = _selected_executor(db, "Reset queue recovery")
    _error_turn(db, plan=plan, task=task, attempt=attempt, commitment_id=commitment_id)
    _held_input(db)
    _provider_reset(db, "turn-error")
    db.execute(
        """UPDATE messages SET write_outcome=NULL, delivery_error=NULL, target_model=NULL
           WHERE id='held-input'""",
    )

    detailed, row = assert_header_parity(db, plan.id)

    assert row["progress"]["liveness"] == "advancing"
    assert row["display"] == {"label_key": "waiting", "tone_key": "waiting"}
    assert detailed["nodes"][0]["status_reason"] == (
        "Provider resets at 2099-01-01T12:00:00+00:00"
    )


def test_provider_reset_uses_same_shown_condition_before_distribution(db):
    plan = service.ensure_plan("thread", "moving", db=db)
    task = _task(plan.id, "Reset", db=db)
    service.transition_node(task.id, "active", db=db)
    admission.select(task.id, actor_id="moving", provenance="test", db=db)
    row = activity_facts.compact_nodes([plan.id], db=db)[plan.id][0]
    reduced = activity_reducer.reduce_node(row, dict(db.execute("SELECT * FROM plans WHERE id=?", (plan.id,)).fetchone()),
        review_kind=None, decision_wait=False, child_cause=None, executor_stop_fact={"stalled": "error"})
    full_node = {"state": "active", "execution": {"condition": "running", "causes": []},
                 "display": {"label_key": "waiting"}, "review": None, "requires_user_action": False}
    assert reduced.bucket == operator_node_state.leaf_bucket(full_node) == "waiting"
    assert reduced.condition == "running"  # Admission/selection remains unchanged.


def test_executor_stop_facts_chunk_sqlite_reads(db):
    executor_ids = {f"chunk-{index}" for index in range(901)}
    db.executemany(
        """INSERT INTO threads (id,user_id,project_id,title,created_at,updated_at)
           VALUES (?,'user','p',?,'t','t')""",
        [(thread_id, thread_id) for thread_id in executor_ids],
    )
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('chunk-error','chunk-900',0,'native/model','error','t','boot')""",
    )

    facts = operator_stopped.executor_facts(executor_ids, db=db)

    assert facts == {"chunk-900": {"stalled": "error"}}


def test_ordinary_closed_disclosure_and_rejected_branch_suppression(db):
    plan = service.ensure_plan("thread", "moving", db=db)
    closed = _task(plan.id, "Ordinary closed", db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (closed.id,))
    parent = service.add_node(plan.id, kind="phase", title="Rejected", acceptance_criteria="done", db=db)
    child = _task(plan.id, "Old round", parent_id=parent.id, db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='closed', sent_back=1 WHERE id=?", (parent.id,))
    db.execute("""INSERT INTO plan_node_events(id,node_id,event,provenance,payload_json,created_at)
        VALUES ('rejected',?,'review_rejected','test','{}','t')""", (parent.id,))
    detailed, row = assert_header_parity(db, plan.id)
    assert [node["id"] for node in detailed["nodes"]] == [closed.id]
    # Closed work is settled, so the compact read loads no row for it (#731 S2-2): the
    # disclosure is the plan's stored top level, which the header parity above holds equal.
    assert activity_facts.compact_nodes([plan.id], db=db) == {}
    assert row["has_nodes"] is True
    assert parent.id != child.id


@pytest.mark.parametrize(
    ("case", "expected_display"),
    [
        ("all_closed", {"label_key": "finished", "tone_key": "closed"}),
        ("failed_with_accepted", {"label_key": "finished", "tone_key": "failed"}),
        ("zero_nodes", {"label_key": "finished", "tone_key": "closed"}),
        ("paused_terminal", {"label_key": "finished", "tone_key": "complete"}),
    ],
)
def test_terminal_plan_response_uses_canonical_reduction(db, case, expected_display):
    plan = service.ensure_plan("thread", "moving", db=db)
    if case == "all_closed":
        node = _task(plan.id, "Closed", db=db)
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (node.id,))
    elif case == "failed_with_accepted":
        accepted = _task(plan.id, "Accepted", db=db)
        failed = _task(plan.id, "Failed", db=db)
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (accepted.id,))
            db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (failed.id,))
    elif case == "paused_terminal":
        accepted = _task(plan.id, "Accepted", db=db)
        closed = _task(plan.id, "Closed", db=db)
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (accepted.id,))
            db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (closed.id,))
        db.execute("UPDATE plans SET state='paused' WHERE id=?", (plan.id,))

    payload = assert_terminal_response_identity(db)
    if case != "zero_nodes":
        assert_project_response_parity(db)
    row = next(item for item in payload["plans"] if item["plan"]["id"] == plan.id)
    assert row["has_nodes"] is (case != "zero_nodes")
    assert row["display"] == expected_display


def test_closed_reduced_node_uses_canonical_terminal_display(db):
    plan = service.ensure_plan("thread", "moving", db=db)
    closed = _task(plan.id, "Closed", db=db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (closed.id,))
    # A closed row is never loaded by the compact read (it is settled); reduce its own facts.
    row = dict(db.execute("SELECT * FROM plan_nodes WHERE id=?", (closed.id,)).fetchone())
    reduced = activity_reducer.reduce_node(
        row, dict(db.execute("SELECT * FROM plans WHERE id=?", (plan.id,)).fetchone()),
        review_kind=None, decision_wait=False, child_cause=None, executor_stop_fact=None,
    )

    assert {"label_key": reduced.label_key, "tone_key": reduced.tone_key} == (
        operator_display_state.TERMINAL_DISPLAY["closed"]
    )
    assert reduced.bucket == operator_node_state.leaf_bucket_from_facts(
        "closed", "terminal", display_label=reduced.label_key,
    )
