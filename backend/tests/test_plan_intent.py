"""Agreed task intent, validation and inheritance."""
from dataclasses import replace

import pytest

from plans import service
from plans.boundary import PlanActor, mutate
from tests.plan_scene import ACTOR, db  # noqa: F401


def test_node_owner_thread_names_the_plan_owning_thread(db):
    from plans.public import node_owner_thread
    plan = service.ensure_plan("thread", "root", db=db)
    node = service.add_node(plan.id, kind="task", title="Owned", acceptance_criteria="Read", intent="Own it", db=db)
    assert node_owner_thread(node.id, db=db) == "root"
    assert node_owner_thread("no-such-node", db=db) is None


@pytest.mark.parametrize("operation", ("add", "insert"))
def test_add_and_insert_refuse_missing_intent(db, operation):
    plan = service.ensure_plan("thread", "root", db=db)
    with pytest.raises(service.PlanInputError) as refusal:
        mutate(plan.id, operation, {"title": "Ungrounded", "acceptance_criteria": "It is grounded", "size": "S"},
               actor=ACTOR, provenance="test", mutation_id=f"intent-required-{operation}", db=db)
    assert str(refusal.value) == "task creation requires intent"


@pytest.mark.parametrize("operation", ("add", "insert"))
def test_add_and_insert_accept_agreed_intent(db, operation):
    plan = service.ensure_plan("thread", "root", db=db)
    params = {"title": "Agent-started", "acceptance_criteria": "It is done", "size": "S",
              "intent": "Do the work the agent took on"}
    result = mutate(plan.id, operation, params, actor=ACTOR, provenance="test",
                    mutation_id=f"intent-no-quote-{operation}", db=db).result
    assert result["node"]["title"] == "Agent-started"
    assert result["node"]["intent"] == params["intent"]


def test_human_add_requires_intent(db):
    plan = service.ensure_plan("thread", "root", db=db)
    human = PlanActor(None, "user-1", None, kind="user")
    with pytest.raises(service.PlanInputError, match="task creation requires intent"):
        mutate(plan.id, "add", {"title": "Ungrounded", "acceptance_criteria": "It is grounded", "size": "S"},
               actor=human, provenance="test", mutation_id="human-intent-required", db=db)
    result = mutate(plan.id, "add", {
        "title": "Human task", "acceptance_criteria": "Outcome is checked", "size": "S",
        "intent": "The authenticated person created this task",
    }, actor=human, provenance="test", mutation_id="human-creates-task", db=db).result
    assert result["node"]["intent"] == "The authenticated person created this task"


def test_intent_byte_limit(db):
    plan = service.ensure_plan("thread", "root", db=db)
    with pytest.raises(service.PlanInputError) as refusal:
        mutate(plan.id, "add", {"title": "Bounded", "acceptance_criteria": "The bounds are clear", "size": "S",
                                "intent": "i" * 601}, actor=ACTOR, provenance="test", mutation_id="overlong-intent", db=db)
    assert str(refusal.value) == "intent exceeds the 600-byte limit"


def test_editing_intent_bumps_revision_but_description_does_not(db):
    plan = service.ensure_plan("thread", "root", db=db)
    created = mutate(plan.id, "add", {
        "title": "Revision target", "acceptance_criteria": "Outcome is checked", "size": "S",
        "intent": "Keep the original outcome",
    }, actor=ACTOR, provenance="test", mutation_id="revision-create", db=db).result["node"]
    intent_edit = mutate(plan.id, "edit", {
        "node_id": created["id"], "expected_revision": created["revision"], "intent": "Keep the corrected outcome",
    }, actor=ACTOR, provenance="test", mutation_id="revision-intent", db=db).result["node"]
    description_edit = mutate(plan.id, "edit", {
        "node_id": created["id"], "expected_revision": intent_edit["revision"],
        "description": "Working notes are not requirements.",
    }, actor=ACTOR, provenance="test", mutation_id="revision-description", db=db).result["node"]
    assert (intent_edit["requirements_revision"], description_edit["requirements_revision"]) == (2, 2)
    restatements = db.execute(
        """SELECT json_extract(payload_json,'$.requirements_revision') FROM plan_node_events
           WHERE node_id=? AND event='node_restated' ORDER BY rowid""", (created["id"],),
    ).fetchall()
    assert [row[0] for row in restatements] == [2, 2]


def test_creation_paths_inherit_the_canonical_intent_and_exempt_the_gate(db):
    from plans import mutations
    root_plan = service.ensure_plan("thread", "root", db=db)
    canonical = service.add_node(root_plan.id, kind="task", title="Canonical", intent="Preserve the outcome",
                                 acceptance_criteria="The canonical outcome is proved", db=db)
    source = service.add_node(root_plan.id, kind="task", title="Inherited source", intent_node_id=canonical.id,
                              acceptance_criteria="The inherited source is proved", db=db)
    split = mutate(root_plan.id, "split", {
        "node_id": source.id, "title": "Split task", "acceptance_criteria": "Split proof", "size": "S",
        "expected_revision": source.revision,
    }, actor=ACTOR, provenance="test", mutation_id="intent-split", db=db).result["node"]
    follow_up = mutate(root_plan.id, "add_follow_up", {
        "node_id": source.id, "title": "Follow-up", "acceptance_criteria": "Follow-up proof", "size": "S",
        "expected_revision": service.get_node(source.id, db=db).revision,
    }, actor=ACTOR, provenance="test", mutation_id="intent-follow-up", db=db).result["node"]
    service.transition_node(source.id, "active", db=db)
    mutations.attach_spawned_thread(source.id, "child", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    attached = mutate(child_plan.id, "add", {
        "title": "Attached worker step", "acceptance_criteria": "Worker proof", "size": "S",
    }, actor=replace(ACTOR, thread_id="child"), provenance="test", mutation_id="intent-attached-worker", db=db).result["node"]
    accept_source = service.add_node(root_plan.id, kind="task", title="Accept source", intent_node_id=canonical.id,
                                     acceptance_criteria="The accepted source is proved", db=db)
    service.transition_node(accept_source.id, "active", db=db)
    reviewed = mutate(root_plan.id, "request_review", {
        "node_id": accept_source.id, "expected_revision": service.get_node(accept_source.id, db=db).revision,
        "evidence": "The source is ready for review.",
    }, actor=ACTOR, provenance="test", mutation_id="intent-review", db=db).result["node"]
    accepted_follow_up = mutate(root_plan.id, "accept_with_follow_up", {
        "node_id": accept_source.id, "expected_revision": reviewed["revision"],
        "title": "Accepted follow-up", "acceptance_criteria": "Accepted follow-up proof", "size": "S",
        "verification_result": "passed",
    }, actor=ACTOR, provenance="test", mutation_id="intent-accepted-follow-up", db=db).result["follow_up"]
    assert {service.get_node(node["id"], db=db).intent_node_id
            for node in (split, follow_up, attached, accepted_follow_up)} == {canonical.id}


def test_plan_read_exposes_canonical_intent(db):
    from plans.current import read_current_status
    from plans.public import intent_of
    plan = service.ensure_plan("thread", "root", db=db)
    canonical = service.add_node(plan.id, kind="task", title="Canonical intent", intent="Keep the request aligned",
                                 acceptance_criteria="The request is satisfied", db=db)
    inherited = service.add_node(plan.id, kind="task", title="Inherited detail", intent_node_id=canonical.id,
                                 acceptance_criteria="The inherited request is satisfied", db=db)
    detail = read_current_status(actor=ACTOR, node_id=inherited.id, db=db)
    assert detail["canonical_intent"] == {"node_id": canonical.id, "title": "Canonical intent",
                                           "intent": "Keep the request aligned"}
    assert intent_of(inherited.id, db=db) == {
        "node_id": canonical.id, "title": "Canonical intent", "requirements_revision": 1,
        "intent": "Keep the request aligned", "acceptance_criteria": "The request is satisfied",
    }
