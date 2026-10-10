"""Direct, sequenced plan-event transcript contracts."""

from __future__ import annotations

import json
import sqlite3

import pytest

from infra import queries
from infra.thread_driver import admit_input
from plans import lifecycle, notifications, service
from plans.boundary import PlanActor, mutate
from tests.plan_review_helpers import submit_review_evidence


@pytest.fixture
def db(isolated_db):
    connection = sqlite3.connect(isolated_db)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    connection.execute(
        "INSERT INTO threads "
        "(id,user_id,project_id,model,created_at,updated_at) "
        "VALUES ('thread','user','project','native/model','t','t')"
    )
    connection.commit()
    yield connection
    connection.close()


def _plan(db):
    plan = service.ensure_plan("thread", "thread", db=db)
    db.commit()
    return plan


def _metadata(row) -> dict:
    return json.loads(row["metadata"] or "{}")


def test_owner_authored_pause_is_one_sequenced_activity_receipt(db):
    plan = _plan(db)

    lifecycle.transition(
        plan.id,
        "paused",
        expected_revision=plan.revision,
        actor_id="thread",
        provenance="test",
        db=db,
    )

    row = db.execute(
        "SELECT role,content,metadata,delivery_state,seq FROM messages"
    ).fetchone()
    assert row["role"] == "system"
    assert row["content"] == "Plan paused."
    assert row["delivery_state"] == "delivered"
    assert row["seq"] == 1
    metadata = _metadata(row)
    assert metadata["type"] == "activity_receipt"
    assert metadata["event_type"] == "plan.paused"
    assert metadata["origin"] == "plan"
    assert metadata["plan_id"] == plan.id
    assert metadata["summary"] == "Plan paused."
    assert db.execute(
        "SELECT COUNT(*) FROM continuation_triggers WHERE kind='plan_transcript'"
    ).fetchone()[0] == 0


def test_external_pause_appends_one_passive_change_and_does_not_start_a_turn(db):
    plan = _plan(db)

    lifecycle.transition(
        plan.id,
        "paused",
        expected_revision=plan.revision,
        actor_id="user:user",
        provenance="api.plans",
        db=db,
    )

    # The write also journals the owner's thin status (plan.status); the pause is its own row.
    row = db.execute(
        "SELECT * FROM context_change_journal WHERE change_kind<>'plan.status'"
    ).fetchone()
    assert row["object_kind"] == "plan"
    assert row["object_id"] == plan.id
    assert row["change_kind"] == "plan.paused"
    assert row["summary"] == "Plan paused."
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM active_turns WHERE status='running'"
    ).fetchone()[0] == 0


def test_next_user_input_claims_the_passive_event_first(db):
    plan = _plan(db)
    lifecycle.transition(
        plan.id,
        "paused",
        expected_revision=plan.revision,
        actor_id="user:user",
        provenance="api.plans",
        db=db,
    )

    db.commit()
    admission = admit_input(
        "thread",
        role="user",
        content="continue",
        intent="apply_now",
        message_id="user-input",
        turn_id="turn-1",
        boot_epoch="boot-1",
        admitted_at="2026-08-28T12:00:00+00:00",
        db=db,
    )

    assert admission.batch is not None
    rows = db.execute(
        "SELECT id,input_order,active_turn_id FROM messages ORDER BY input_order"
    ).fetchall()
    assert admission.passive is not None
    assert [row["input_order"] for row in rows] == [1, 2]
    assert rows[0]["id"] == admission.passive.message_id
    assert rows[1]["id"] == "user-input"
    assert {row["active_turn_id"] for row in rows} == {"turn-1"}


def test_only_top_level_terminal_milestones_enter_the_transcript(db):
    plan = _plan(db)
    parent = service.add_node(
        plan.id, kind="phase", title="Parent", acceptance_criteria="Done", db=db,
    )
    leaf = service.add_node(
        plan.id,
        kind="task",
        title="Leaf",
        acceptance_criteria="Done",
        parent_id=parent.id,
        db=db,
    )
    top = service.add_node(
        plan.id, kind="task", title="Top", acceptance_criteria="Done", db=db,
    )
    for node in (leaf, top):
        service.transition_node(node.id, "active", actor_id="thread", db=db)
        submit_review_evidence(node.id, db=db, actor_id="thread")
        service.transition_node(
            node.id,
            "accepted",
            actor_id="thread",
            verification_result="passed",
            verified_by="thread",
            db=db,
        )

    rows = db.execute("SELECT content,metadata FROM messages ORDER BY seq").fetchall()
    assert [row["content"] for row in rows] == ["Top-level milestone accepted: Top"]
    assert _metadata(rows[0])["type"] == "activity_receipt"


def test_deferred_mutation_broadcasts_the_exact_committed_row(db, monkeypatch):
    plan = _plan(db)
    queries.insert_message("thread", "user", "Ship it", {}, "user-word", db=db)
    # An owner-authored top-level milestone is one receipt in its transcript.
    top = service.add_node(
        plan.id, kind="task", title="Top", acceptance_criteria="Done", db=db,
    )
    service.transition_node(top.id, "active", actor_id="thread", db=db)
    submit_review_evidence(top.id, db=db, actor_id="thread")
    db.commit()
    actor = PlanActor("thread", "user", "project", turn_id="turn-1")
    sent = []
    monkeypatch.setattr(
        "infra.state.broadcast_to_thread",
        lambda thread_id, payload, **_kwargs: sent.append((thread_id, payload)),
    )

    deferred = mutate(
        plan.id,
        "accept",
        {
            "node_id": top.id, "verification_result": "passed",
            "expected_revision": service._node_row(db, top.id)["revision"],
        },
        actor=actor,
        provenance="tool.plans",
        mutation_id="accept-1",
        db=db,
    )
    assert sent == []

    notifications.deliver_deferred(deferred)

    message_frames = [payload for thread, payload in sent if thread == "thread"]
    assert len(message_frames) == 1
    message = message_frames[0]["message"]
    assert message["seq"] == 2  # after the user's own message
    assert message["metadata"]["type"] == "activity_receipt"


def test_lost_update_does_not_append_a_phantom_plan_event(db, monkeypatch):
    plan = _plan(db)
    original_now = service._now

    def _bump_revision_then_now():
        db.execute(
            "UPDATE plans SET revision=revision+1,updated_at=? WHERE id=?",
            (original_now(), plan.id),
        )
        return original_now()

    monkeypatch.setattr(service, "_now", _bump_revision_then_now)

    with pytest.raises(service.PlanError, match="revision conflict"):
        lifecycle.transition(
            plan.id,
            "paused",
            expected_revision=plan.revision,
            actor_id="thread",
            provenance="test",
            db=db,
        )

    assert db.execute("SELECT COUNT(*) FROM plan_events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
