"""Fail-first contract for plan-node-bound artifact registrations."""

from __future__ import annotations

import pytest

from infra import config
from infra.database import get_db
from plans import notifications, service
from plans.boundary import PlanActor, mutate
from registry import models, pinning, view
from registry.registration_writer import RegistrationOrigin
from tests.plan_review_helpers import submit_review_evidence
from tests.registry_helpers import make_session


def _node_and_turn(db, thread_id: str, *, turn_id: str):
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title="Produce artifact",
        acceptance_criteria="Artifact is produced and verified",
        db=db,
    )
    node = service.transition_node(node.id, "active", db=db)
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,0,'native/test','running','t0','boot',?,?,?,?,?)""",
        (
            turn_id,
            thread_id,
            plan.id,
            node.id,
            f"attempt-{turn_id}",
            node.commitment_id,
            "execution",
        ),
    )
    db.commit()
    return plan, node


def _origin(thread_id: str, turn_id: str = "turn-1"):
    return RegistrationOrigin(thread_id, turn_id, "artifact_tool")


def _register(db, thread_id: str, external_id: str, *, origin=None, **kwargs):
    return models.register_artifact(
        integration="workspace",
        type="document",
        external_id=external_id,
        parent_thread_id=thread_id,
        title=external_id,
        origin=origin,
        db=db,
        **kwargs,
    )


def _registration(db, artifact_id: str, thread_id: str):
    return db.execute(
        """SELECT * FROM artifact_registrations
           WHERE artifact_id=? AND parent_thread_id=?""",
        (artifact_id, thread_id),
    ).fetchone()


def _accept(db, node_id: str) -> None:
    service.transition_node(node_id, "review_requested", db=db)
    submit_review_evidence(node_id, db=db)
    service.transition_node(
        node_id,
        "accepted",
        verification_result="passed",
        verified_by="reviewer",
        db=db,
    )


@pytest.mark.usefixtures("isolated_db")
def test_exact_live_turn_binds_and_acceptance_retires_only_registration(monkeypatch):
    db = get_db()
    telemetry = []
    monkeypatch.setattr(
        "registry.origin_telemetry.emit_product_event",
        lambda category, action, **kwargs: telemetry.append(
            (category, action, kwargs)
        ),
    )
    thread_id = make_session(db, prefix="origin")
    _plan, node = _node_and_turn(db, thread_id, turn_id="turn-1")
    artifact = _register(db, thread_id, "bound.md", origin=_origin(thread_id))

    before = _registration(db, artifact["id"], thread_id)
    assert before["origin_plan_node_id"] == node.id
    assert before["retired_at"] is None
    assert telemetry[0][0:2] == ("artifact_origin", "recorded")
    assert telemetry[0][2]["properties"]["outcome"] == "bound"
    assert telemetry[0][2]["properties"]["producer"] == "artifact_tool"

    _accept(db, node.id)

    after = _registration(db, artifact["id"], thread_id)
    assert after["surfacing"] == "settled"
    assert after["retired_at"] is not None
    pane = view.pane_for_thread(thread_id)
    assert artifact["id"] not in {
        item["id"] for item in pane["nodes"] if item["kind"] == "artifact"
    }
    assert pane["history"] == []
    assert models.get_artifact(artifact["id"], db=db)["state"] == "active"


@pytest.mark.usefixtures("isolated_db")
def test_untrusted_wrong_thread_and_terminal_turn_origins_remain_unbound():
    db = get_db()
    owner = make_session(db, prefix="origin-owner")
    other = make_session(db, prefix="origin-other")
    _plan, node = _node_and_turn(db, owner, turn_id="turn-1")

    untrusted = _register(db, owner, "untrusted.md")
    wrong_target = _register(
        db, other, "wrong-target.md", origin=_origin(owner)
    )
    service.transition_node(node.id, "failed", db=db)
    after_terminal = _register(
        db, owner, "terminal.md", origin=_origin(owner)
    )

    assert _registration(db, untrusted["id"], owner)["origin_plan_node_id"] is None
    assert _registration(db, wrong_target["id"], other)["origin_plan_node_id"] is None
    assert _registration(db, after_terminal["id"], owner)["origin_plan_node_id"] is None
    assert db.execute(
        "SELECT plan_node_id FROM active_turns WHERE id='turn-1'"
    ).fetchone()[0] == node.id


@pytest.mark.usefixtures("isolated_db")
def test_boundary_delivers_artifact_invalidation_only_after_commit(monkeypatch):
    db = get_db()
    thread_id = make_session(db, prefix="delivery")
    user_id = db.execute(
        "SELECT user_id FROM threads WHERE id=?", (thread_id,)
    ).fetchone()[0]
    plan, node = _node_and_turn(db, thread_id, turn_id="turn-delivery")
    artifact = _register(
        db,
        thread_id,
        "delivery.md",
        origin=_origin(thread_id, "turn-delivery"),
    )
    reviewed = submit_review_evidence(node.id, db=db)
    sent = []
    retirement_telemetry = []
    monkeypatch.setattr(
        "infra.product_events.emit_product_event",
        lambda category, action, **kwargs: retirement_telemetry.append(
            (category, action, kwargs)
        ),
    )
    monkeypatch.setattr(
        notifications.state,
        "broadcast_to_user",
        lambda target_user_id, message: sent.append((target_user_id, message)),
    )

    deferred = mutate(
        plan.id,
        "accept",
        {
            "node_id": node.id,
            "verification_result": "passed",
            "expected_revision": reviewed.revision,
        },
        actor=PlanActor(thread_id, user_id, None),
        provenance="test.artifact_node_lifecycle",
        mutation_id="accept-delivery",
        db=db,
    )

    assert sent == []
    assert deferred.artifact_messages == (
        (
            user_id,
            {
                "type": "artifacts_changed",
                "artifact_id": artifact["id"],
                "owner_thread_id": thread_id,
                "affected_thread_ids": [thread_id],
            },
        ),
    )
    notifications.deliver_deferred(deferred)
    assert [message["type"] for _recipient, message in sent] == [
        "plans_changed",
        "artifacts_changed",
    ]
    retirements = [
        event for event in retirement_telemetry
        if event[0:2] == ("artifact_pane", "auto_retired")
    ]
    assert len(retirements) == 1
    assert retirements[0][2]["properties"]["retired_count"] == 1


@pytest.mark.usefixtures("isolated_db")
def test_failed_and_follow_up_settlement_retire_bound_registrations():
    db = get_db()
    failed_thread = make_session(db, prefix="failed")
    _failed_plan, failed_node = _node_and_turn(
        db, failed_thread, turn_id="turn-failed"
    )
    failed_artifact = _register(
        db,
        failed_thread,
        "failed.md",
        origin=_origin(failed_thread, "turn-failed"),
    )

    service.transition_node(failed_node.id, "failed", db=db)

    failed_row = _registration(db, failed_artifact["id"], failed_thread)
    assert failed_row["surfacing"] == "settled"
    assert failed_row["retired_at"] is not None

    follow_thread = make_session(db, prefix="follow-up")
    _follow_plan, follow_node = _node_and_turn(
        db, follow_thread, turn_id="turn-follow-up"
    )
    follow_artifact = _register(
        db,
        follow_thread,
        "follow-up.md",
        origin=_origin(follow_thread, "turn-follow-up"),
    )
    submit_review_evidence(follow_node.id, db=db)

    service.accept_with_follow_up(
        follow_node.id,
        title="Residual task",
        acceptance_criteria="Residual work is complete",
        verification_result="passed",
        verified_by="reviewer",
        db=db,
    )

    follow_row = _registration(db, follow_artifact["id"], follow_thread)
    assert follow_row["surfacing"] == "settled"
    assert follow_row["retired_at"] is not None


@pytest.mark.usefixtures("isolated_db")
def test_settlement_stamps_quiet_without_changing_surfacing_or_visibility_event():
    db = get_db()
    thread_id = make_session(db, prefix="quiet")
    _plan, node = _node_and_turn(db, thread_id, turn_id="turn-q")
    artifact = _register(
        db, thread_id, "quiet.md", origin=_origin(thread_id, "turn-q")
    )
    models.update_surfacing(
        artifact["id"], surfacing="quiet", parent_thread_id=thread_id, db=db
    )
    before_count = db.execute(
        """SELECT COUNT(*) FROM artifact_registration_events
           WHERE artifact_id=? AND event_type='surfacing_changed'""",
        (artifact["id"],),
    ).fetchone()[0]

    _accept(db, node.id)

    row = _registration(db, artifact["id"], thread_id)
    assert row["surfacing"] == "quiet"
    assert row["retired_at"] is not None
    assert db.execute(
        """SELECT COUNT(*) FROM artifact_registration_events
           WHERE artifact_id=? AND event_type='origin_node_retired'""",
        (artifact["id"],),
    ).fetchone()[0] == 1
    assert db.execute(
        """SELECT COUNT(*) FROM artifact_registration_events
           WHERE artifact_id=? AND event_type='surfacing_changed'""",
        (artifact["id"],),
    ).fetchone()[0] == before_count


@pytest.mark.usefixtures("isolated_db")
def test_reregistration_revives_settled_but_preserves_quiet_and_rebinds_caller_only():
    db = get_db()
    owner = make_session(db, prefix="owner")
    subscriber = make_session(db, prefix="subscriber")
    _plan_a, node_a = _node_and_turn(db, owner, turn_id="turn-a")
    artifact = _register(
        db, owner, "shared.md", origin=_origin(owner, "turn-a")
    )
    _accept(db, node_a.id)
    owner_settled = dict(_registration(db, artifact["id"], owner))

    _plan_b, node_b = _node_and_turn(db, subscriber, turn_id="turn-b")
    _register(
        db, subscriber, "shared.md", origin=_origin(subscriber, "turn-b")
    )

    owner_after = _registration(db, artifact["id"], owner)
    subscriber_after = _registration(db, artifact["id"], subscriber)
    assert owner_after["origin_plan_node_id"] == owner_settled["origin_plan_node_id"]
    assert owner_after["retired_at"] == owner_settled["retired_at"]
    assert owner_after["surfacing"] == "settled"
    assert subscriber_after["origin_plan_node_id"] == node_b.id
    assert subscriber_after["surfacing"] == "visible"

    _accept(db, node_b.id)
    _register(db, subscriber, "shared.md")
    revived = _registration(db, artifact["id"], subscriber)
    assert revived["origin_plan_node_id"] is None
    assert revived["retired_at"] is None
    assert revived["surfacing"] == "visible"

    models.update_surfacing(
        artifact["id"], surfacing="quiet", parent_thread_id=subscriber, db=db
    )
    _register(db, subscriber, "shared.md")
    assert _registration(db, artifact["id"], subscriber)["surfacing"] == "quiet"


@pytest.mark.usefixtures("isolated_db")
def test_pinned_artifact_survives_real_node_settlement():
    db = get_db()
    thread_id = make_session(db, prefix="pinned-settlement")
    _plan, node = _node_and_turn(db, thread_id, turn_id="turn-pinned")
    artifact = _register(
        db, thread_id, "pinned.md", origin=_origin(thread_id, "turn-pinned")
    )
    pinning.update_pinned(
        artifact["id"], pinned=True, parent_thread_id=thread_id, db=db,
    )

    _accept(db, node.id)

    # Settlement still stamps the permanent retirement fact even though the
    # row is pinned -- it just leaves surfacing alone, since pin's job is to
    # override display, not to erase that the task actually finished.
    row = _registration(db, artifact["id"], thread_id)
    assert row["relationship_state"] == "owner-pinned"
    assert row["surfacing"] == "visible"
    assert row["retired_at"] is not None
    assert db.execute(
        """SELECT COUNT(*) FROM artifact_registration_events
           WHERE artifact_id=? AND event_type='origin_node_retired'""",
        (artifact["id"],),
    ).fetchone()[0] == 1

    # Unpinning later finds the fact waiting and applies it.
    pinning.update_pinned(
        artifact["id"], pinned=False, parent_thread_id=thread_id, db=db,
    )
    unpinned = _registration(db, artifact["id"], thread_id)
    assert unpinned["surfacing"] == "settled"
    assert unpinned["retired_at"] == row["retired_at"]


@pytest.mark.usefixtures("isolated_db")
def test_pinning_revives_settled_artifacts_without_overriding_quiet():
    db = get_db()
    thread_id = make_session(db, prefix="pin-revive")
    _plan, node = _node_and_turn(db, thread_id, turn_id="turn-revive")
    visible = _register(
        db, thread_id, "visible.md", origin=_origin(thread_id, "turn-revive")
    )
    quiet = _register(
        db, thread_id, "quiet.md", origin=_origin(thread_id, "turn-revive")
    )
    models.update_surfacing(
        quiet["id"], surfacing="quiet", parent_thread_id=thread_id, db=db,
    )

    _accept(db, node.id)

    for artifact in (visible, quiet):
        assert _registration(db, artifact["id"], thread_id)["retired_at"] is not None
        pinning.update_pinned(
            artifact["id"], pinned=True, parent_thread_id=thread_id, db=db,
        )

    visible_row = _registration(db, visible["id"], thread_id)
    quiet_row = _registration(db, quiet["id"], thread_id)
    # Pin only unhides; retired_at is the permanent settlement record
    # and stays exactly as settlement wrote it for both rows.
    assert visible_row["surfacing"] == "visible"
    assert visible_row["retired_at"] is not None
    assert quiet_row["surfacing"] == "quiet"
    assert quiet_row["retired_at"] is not None


@pytest.mark.usefixtures("isolated_db")
def test_settlement_spares_shared_documents_but_retires_task_scoped_files():
    db = get_db()
    thread_id = make_session(db, prefix="retirement-scope")
    _plan, node = _node_and_turn(db, thread_id, turn_id="turn-scope")
    durable_workspace = _register(
        db,
        thread_id,
        "workspace://ws-1/shared/reference.md",
        path=str(config.WORKSPACE / "shared" / "reference.md"),
        origin=_origin(thread_id, "turn-scope"),
        retention="durable",
        metadata={"zone": "workspace"},
    )
    own_folder = _register(
        db,
        thread_id,
        "workspace://ws-1/.threads/t1/notes.md",
        origin=_origin(thread_id, "turn-scope"),
        retention="durable",
        metadata={"zone": "workspace"},
    )
    working_tray = _register(
        db,
        thread_id,
        "working/scratch.md",
        origin=_origin(thread_id, "turn-scope"),
        retention="ephemeral",
        metadata={"zone": "workspace"},
    )
    repo_file = _register(
        db,
        thread_id,
        "repo/implementation.py",
        origin=_origin(thread_id, "turn-scope"),
        retention="durable",
        metadata={"zone": "repo"},
    )

    _accept(db, node.id)

    durable_row = _registration(db, durable_workspace["id"], thread_id)
    assert durable_row["surfacing"] == "visible"
    assert durable_row["retired_at"] is None
    for artifact in (own_folder, working_tray, repo_file):
        row = _registration(db, artifact["id"], thread_id)
        assert row["surfacing"] == "settled"
        assert row["retired_at"] is not None
