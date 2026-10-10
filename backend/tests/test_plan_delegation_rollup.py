"""P4c red-first tests for child-plan attachment, rollup, and harvest."""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest

from automation import plan_liveness
from automation.continuation.sources.harvest import HarvestSource
from infra.schema import SCHEMA_SQL
import plans.delegation as delegation
import plans.mutations as mutations
import plans.rollup as rollup
import plans.service as service
from tests.plan_review_helpers import submit_review_evidence
from tests.plan_writer_authorizer import raw_plan_seed


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for project_id in ("project-1", "project-2"):
        connection.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,'t','t')",
            (project_id, project_id),
        )
    for thread_id, project_id, spawned_by in (
        ("parent", "project-1", None),
        ("child", "project-1", "parent"),
        ("other-child", "project-1", "parent"),
        ("cross-project-child", "project-2", "parent"),
    ):
        connection.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1',?,?,'test/model','t','t')""",
            (thread_id, project_id, spawned_by),
        )
    connection.commit()
    yield connection
    connection.close()


def _task(plan_id, title, *, db, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} DOD", db=db, **kwargs,
    )


def _accept(node_id, *, db, verifier="parent"):
    service.transition_node(node_id, "active", db=db)
    submit_review_evidence(node_id, db=db)
    service.transition_node(
        node_id, "accepted", verification_result="passed",
        verified_by=verifier, db=db,
    )


def _parent_spawn_node(db, title="Delegate"):
    plan = service.ensure_plan("thread", "parent", db=db)
    node = _task(plan.id, title, spawn_kind="claude-code", db=db)
    service.transition_node(node.id, "active", db=db)
    return plan, node


def _stage_legacy_attachment(node_id, child_id, *, db, payload=None):
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET spawned_thread_id=? WHERE id=?",
            (child_id, node_id),
        )
    event_id = service._event(
        db, node_id, "spawned_thread_attached", actor_id=None,
        provenance="test.p3_attachment",
        payload=payload or {"spawned_thread_id": child_id},
    )
    db.commit()
    return event_id


def test_child_plan_created_before_attachment_gets_trusted_parent_and_floor(db):
    child_plan = service.ensure_plan("thread", "child", db=db)
    _task(child_plan.id, "Existing child work", db=db)
    parent_plan, parent_node = _parent_spawn_node(db)

    attached = mutations.attach_spawned_thread(
        parent_node.id, "child", db=db,
    )
    child_row = service._plan_row(db, child_plan.id)
    event = db.execute(
        """SELECT * FROM plan_node_events
           WHERE node_id=? AND event='spawned_thread_attached'""",
        (parent_node.id,),
    ).fetchone()

    assert attached.spawned_thread_id == "child"
    assert child_row["parent_plan_id"] == parent_plan.id
    assert event is not None
    payload = delegation.attachment_payload(event)
    assert payload["child_plan_id"] == child_plan.id
    assert payload["event_floor_revision"] == child_row["revision"]


def test_attachment_rejects_cross_project_and_second_open_node(db):
    parent_plan, first = _parent_spawn_node(db, "First")
    mutations.attach_spawned_thread(first.id, "child", db=db)
    second = _task(
        parent_plan.id, "Second", spawn_kind="claude-code", db=db,
    )
    service.transition_node(second.id, "active", db=db)

    with pytest.raises(service.PlanError, match="open structured attachment"):
        mutations.attach_spawned_thread(second.id, "child", db=db)
    with pytest.raises(service.PlanError, match="cross-project"):
        mutations.attach_spawned_thread(second.id, "cross-project-child", db=db)


def test_parent_rollup_shows_live_child_plan_progress_read_only(db):
    parent_plan, parent_node = _parent_spawn_node(db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    child_task = _task(
        child_plan.id, "Child step", db=db,
    )
    mutations.attach_spawned_thread(parent_node.id, "child", db=db)

    projected = rollup.rollup_thread(parent_plan.id, db=db)
    parent = next(node for node in projected.nodes if node["id"] == parent_node.id)

    assert parent["child_plan"].plan["id"] == child_plan.id
    assert parent["child_plan"].progress["total"] == 1
    assert parent["child_plan"].nodes[0]["id"] == child_task.id


def test_attachment_floor_prevents_old_terminal_history_then_harvests_once(
    db, monkeypatch,
):
    child_plan = service.ensure_plan("thread", "child", db=db)
    old = _task(child_plan.id, "Old terminal", db=db)
    _accept(old.id, db=db)
    _parent_plan, parent_node = _parent_spawn_node(db)
    mutations.attach_spawned_thread(parent_node.id, "child", db=db)

    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == []
    assert service.get_node(parent_node.id, db=db).state == "active"

    fresh = _task(child_plan.id, "Fresh epoch", db=db)
    _accept(fresh.id, db=db)
    harvested = delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[])
    replay = delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[])

    assert harvested == [parent_node.id]
    assert replay == []
    assert service.get_node(parent_node.id, db=db).state == "review_requested"
    wake = plan_liveness.on_turn_complete("parent", db=db)
    assert wake is not None
    assert wake.last_open_count == 1
    assert db.execute(
        """SELECT COUNT(*) FROM plan_node_events
           WHERE node_id=? AND event='child_plan_completed'""",
        (parent_node.id,),
    ).fetchone()[0] == 1
    causal = db.execute(
        """SELECT child.node_id FROM plan_node_events parent
           JOIN plan_node_events child ON child.id=parent.causal_event_id
           WHERE parent.node_id=? AND parent.event='child_plan_completed'""",
        (parent_node.id,),
    ).fetchone()
    assert causal["node_id"] == fresh.id


def test_malformed_completed_epoch_does_not_abort_harvest_or_material_sweep(
    db, monkeypatch,
):
    child_plan = service.ensure_plan("thread", "child", db=db)
    fresh = _task(
        child_plan.id, "Fresh epoch", db=db,
    )
    _parent_plan, parent_node = _parent_spawn_node(db)
    mutations.attach_spawned_thread(parent_node.id, "child", db=db)
    db.execute(
        """INSERT INTO plan_node_events
           (id,node_id,actor_id,provenance,event,payload_json,created_at)
           VALUES ('malformed-completion',?,NULL,'test.corruption',
                   'child_plan_completed','{not-json','t')""",
        (parent_node.id,),
    )
    db.commit()
    _accept(fresh.id, db=db)

    HarvestSource().prepare(service._now(), db=db)

    # The accepted step was the child's final return: the harvest carried it.
    assert service.get_node(parent_node.id, db=db).state == "review_requested"
    assert db.execute(
        """SELECT COUNT(*) FROM plan_node_events
           WHERE node_id=? AND event='child_plan_completed'""",
        (parent_node.id,),
    ).fetchone()[0] == 2


def test_harvest_uses_actionable_leaves_not_pending_container_nodes(
    db, monkeypatch,
):
    child_plan = service.ensure_plan("thread", "child", db=db)
    phase = service.add_node(
        child_plan.id, kind="phase", title="Child phase",
        acceptance_criteria="Child phase DOD", db=db,
    )
    leaf = _task(
        child_plan.id, "Nested child work", parent_id=phase.id,
        db=db,
    )
    _parent_plan, parent_node = _parent_spawn_node(db)
    mutations.attach_spawned_thread(parent_node.id, "child", db=db)

    _accept(leaf.id, db=db)

    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [parent_node.id]
    assert service.get_node(parent_node.id, db=db).state == "review_requested"


def test_legacy_attachment_is_not_a_structured_epoch(db):
    _parent_plan, parent_node = _parent_spawn_node(db)
    _stage_legacy_attachment(parent_node.id, "child", db=db)

    assert not delegation.has_open_structured_attachment("child", db=db)
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == []


def test_malformed_and_ineligible_legacy_attachments_suppress_nothing(db):
    _parent_plan, malformed = _parent_spawn_node(db, "Malformed")
    with raw_plan_seed(db):
        db.execute(
            "UPDATE plan_nodes SET spawned_thread_id='child' WHERE id=?",
            (malformed.id,),
        )
    db.execute(
        """INSERT INTO plan_node_events
           (id,node_id,actor_id,provenance,event,payload_json,created_at)
           VALUES ('malformed-legacy',?,NULL,'test.p3_attachment',
                   'spawned_thread_attached','{not-json','t')""",
        (malformed.id,),
    )
    ineligible = _task(
        malformed.plan_id, "Cross project", spawn_kind="claude-code", db=db,
    )
    service.transition_node(ineligible.id, "active", db=db)
    _stage_legacy_attachment(
        ineligible.id, "cross-project-child", db=db,
    )

    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == []
    assert not delegation.has_open_structured_attachment("child", db=db)
    assert not delegation.has_open_structured_attachment(
        "cross-project-child", db=db,
    )


def test_ambiguous_open_legacy_row_invalidates_otherwise_structured_epoch(db):
    parent_plan, structured = _parent_spawn_node(db, "Structured")
    mutations.attach_spawned_thread(structured.id, "child", db=db)
    legacy = _task(
        parent_plan.id, "Legacy duplicate", spawn_kind="claude-code", db=db,
    )
    service.transition_node(legacy.id, "active", db=db)
    _stage_legacy_attachment(legacy.id, "child", db=db)

    assert not delegation.has_open_structured_attachment("child", db=db)
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == []


def test_parent_completion_is_suppressed_only_for_open_attachment(
    isolated_db, monkeypatch,
):
    from core.turn_core import persist_turn_start, persist_turn_terminal
    from infra.database import get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.queries import insert_message
    from infra.thread_driver import admit_input
    from plans import mutations as live_mutations

    live_db = get_db()
    try:
        live_db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('project-cc','Project','t','t')"
        )
        for thread_id, spawned_by in (("parent-cc", None), ("child-cc", "parent-cc")):
            live_db.execute(
                """INSERT INTO threads
                   (id,user_id,project_id,spawned_by,model,created_at,updated_at)
                   VALUES (?,'user','project-cc',?,'claude-code/test','t','t')""",
                (thread_id, spawned_by),
            )
        parent_plan = service.ensure_plan("thread", "parent-cc", db=live_db)
        node = _task(
            parent_plan.id, "Structured", spawn_kind="claude-code", db=live_db,
        )
        service.transition_node(node.id, "active", db=live_db)
        live_mutations.attach_spawned_thread(node.id, "child-cc", db=live_db)
        live_db.commit()
    finally:
        live_db.close()

    structured = admit_input(
        "child-cc",
        role="user",
        content="structured work",
        intent="apply_now",
        message_id="input-structured",
        turn_id="turn-structured",
        boot_epoch="boot",
        admitted_at="t1",
    )
    assert structured.batch is not None
    persist_turn_start("turn-structured", "child-cc", "claude-code/test", "preview")
    begin_batch_write(
        structured.batch, attempt_id="write-structured", started_at="t1",
    )
    settle_batch_write("write-structured", outcome="accepted")
    insert_message(
        "child-cc", "assistant", "done",
        metadata={"turn_id": "turn-structured"},
    )
    persist_turn_terminal("turn-structured", "completed")

    live_db = get_db()
    try:
        assert live_db.execute(
            "SELECT 1 FROM messages WHERE id='parent-completion:turn-structured'"
        ).fetchone() is None
    finally:
        live_db.close()
