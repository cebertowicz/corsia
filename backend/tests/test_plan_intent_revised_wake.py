"""A canonical task's revisions reach only live workers that still need them."""

from __future__ import annotations

import json

import pytest

from plans import admission, holds, mutations, service, system_user_steps
from tests.child_parent_notice_helpers import seeded_db, wakes


@pytest.fixture
def db():
    connection = seeded_db()
    yield connection
    connection.close()


def _delegated_source(db):
    parent = service.ensure_plan("thread", "parent", db=db)
    source = service.add_node(
        parent.id, kind="task", title="Ship the brief", intent="Initial intent",
        acceptance_criteria="Initial criteria", size="S", spawn_kind="codex", db=db,
    )
    service.transition_node(source.id, "active", actor_id="parent", db=db)
    service.ensure_plan("thread", "worker", db=db)
    mutations.attach_spawned_thread(source.id, "worker", actor_id="parent", db=db)
    return source


def _edit(db, node_id, *, actor="parent", **changes):
    return mutations.edit_node(
        node_id, expected_revision=service.get_node(node_id, db=db).revision,
        actor_id=actor, db=db, **changes,
    )


def _wake_payload(db):
    rows = wakes(db, "worker")
    assert len(rows) == 1
    line, payload = rows[0]["content"].split("\n", 1)
    return line, json.loads(payload)["intent_revision"]


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        ({"intent": "Revised intent"}, "Revised intent"),
        ({"acceptance_criteria": "Revised criteria"}, "Initial intent"),
        ({"intent_words": [{
            "quote": "The new constraint", "author": "agent", "role": "bears_on",
            "source_kind": "message", "source_id": "parent",
        }]}, "Initial intent"),
    ),
)
def test_each_requirements_producer_wakes_the_live_worker_once(db, changes, expected):
    source = _delegated_source(db)

    updated = _edit(db, source.id, **changes)

    line, payload = _wake_payload(db)
    assert updated.requirements_revision == 2
    assert "revision 2" in line
    assert payload["requirements_revision"] == 2
    assert payload["intent"] == expected
    assert payload["acceptance_criteria"] == updated.acceptance_criteria
    if "intent_words" in changes:
        assert [word["quote"] for word in payload["intent_words"]] == ["The new constraint"]


def test_description_and_size_edits_do_not_wake(db):
    source = _delegated_source(db)

    _edit(db, source.id, description="Working note")
    _edit(db, source.id, size="M")

    assert wakes(db, "worker") == []


def test_returned_worker_and_editor_are_not_woken(db):
    source = _delegated_source(db)
    service.transition_node(source.id, "review_requested", actor_id="worker", db=db)

    _edit(db, source.id, intent="Never reaches a returned worker")

    assert wakes(db, "worker") == []


def test_editor_recipient_is_not_woken(db):
    source = _delegated_source(db)

    _edit(db, source.id, actor="worker", intent="Worker's own correction")

    assert wakes(db, "worker") == []


def test_user_step_created_from_one_selected_task_does_not_add_an_agent_wake(db):
    plan = service.ensure_plan("thread", "parent", db=db)
    source = service.add_node(
        plan.id, kind="task", title="Source", intent="Initial intent",
        acceptance_criteria="Initial criteria", db=db,
    )
    service.transition_node(source.id, "active", actor_id="parent", db=db)
    assert admission.reconcile_plan(
        plan.id, actor_id="parent", provenance="test", db=db,
    ).selected == ((source.id, "parent"),)

    step = system_user_steps.open_user_step(
        db, "parent", key="browser", title="Enter password", description="Browser handover",
        provenance="test.browser",
    )
    assert service.get_node(step, db=db).intent_node_id == source.id

    _edit(db, source.id, intent="Updated parent intent")

    assert len(wakes(db, "parent")) == 1
    assert wakes(db, "worker") == []


def test_one_worker_with_an_inheriting_step_receives_one_wake(db):
    source = _delegated_source(db)
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    inheriting = service.add_node(
        worker_plan.id, kind="task", title="Implement", acceptance_criteria="Proven",
        intent_node_id=source.id, db=db,
    )
    service.transition_node(inheriting.id, "active", actor_id="worker", db=db)

    _edit(db, source.id, intent="Updated parent intent")

    assert len(wakes(db, "worker")) == 1


@pytest.mark.parametrize("outcome", ("closed", "archived"))
def test_settled_and_archived_inheriting_nodes_do_not_wake_their_executor(db, outcome):
    from plans import archival

    parent = service.ensure_plan("thread", "parent", db=db)
    source = service.add_node(
        parent.id, kind="task", title="Source", intent="Initial intent",
        acceptance_criteria="Initial criteria", db=db,
    )
    service.transition_node(source.id, "active", actor_id="parent", db=db)
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    inheriting = service.add_node(
        worker_plan.id, kind="task", title="Inherited", acceptance_criteria="Proven",
        intent_node_id=source.id, db=db,
    )
    service.transition_node(inheriting.id, "active", actor_id="worker", db=db)
    if outcome == "closed":
        service.transition_node(inheriting.id, "closed", actor_id="worker", db=db)
    else:
        archival.archive_node(inheriting.id, actor_id="worker", db=db)

    _edit(db, source.id, intent="Updated intent")

    assert wakes(db, "worker") == []


def test_paused_delegated_step_keeps_existing_append_suppression(db):
    source = _delegated_source(db)
    holds.pause(
        source.id, "Pause for the user", expected_revision=service.get_node(source.id, db=db).revision,
        actor_id="user-1", provenance="test", db=db,
    )

    _edit(db, source.id, intent="Updated while paused")

    assert wakes(db, "worker") == []


def test_the_last_intent_wake_revision_is_an_indexed_id_range(db):
    from plans import plan_wake_intent

    for thread in ("w1", "w2"):
        db.execute(
            "INSERT OR IGNORE INTO threads (id,user_id,model,title,created_at,updated_at) "
            "VALUES (?,'u-test','claude-code/opus',?,'t','t')", (thread, thread),
        )
    for message_id, thread in (
        ("plan-wake:intent_revised:n1:2:w1", "w1"),
        ("plan-wake:intent_revised:n1:5:w1", "w1"),
        ("plan-wake:intent_revised:n10:9:w1", "w1"),
        ("plan-wake:intent_revised:n1:7:w2", "w2"),
    ):
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at) VALUES (?,?,'user','x','t')",
            (message_id, thread),
        )

    assert plan_wake_intent._last_wake_revision(db, "n1", "w1") == 5
    plan = " ".join(str(row[3]) for row in db.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM messages WHERE id >= ? AND id < ? AND thread_id=?", ("a", "b", "w1"),
    ))
    assert "SCAN messages" not in plan
