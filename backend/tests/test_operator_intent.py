"""The operator shows the reading and the opened task's own done test."""
from plans import followups, mutations, operator_intent, operator_projection, service
from plans.boundary import PlanActor
from tests.test_operator_envelope_contract import _db


def _node(db, thread, node_id):
    envelope = operator_projection.read_thread(thread, actor=PlanActor(None, "user", None, kind="user"), db=db)
    return next(node for plan in envelope["plans"] for node in plan["nodes"] if node["id"] == node_id)


def test_own_run_and_inherited_readings_keep_each_opened_done_test():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    own = service.add_node(plan.id, kind="task", title="Own", acceptance_criteria="Own done", intent="Own reading", db=db)
    run = service.add_node(plan.id, kind="phase", title="Run", acceptance_criteria="Run done", workflow_ref="example", intent="Run reading", db=db)
    step = service.add_node(plan.id, kind="task", title="Step", acceptance_criteria="Step done", parent_id=run.id, workflow_step="step", difficulty="routine", intent_node_id=run.id, db=db)
    detached = service.add_node(plan.id, kind="task", title="Detached step", acceptance_criteria="Detached done", parent_id=run.id, workflow_step="another", difficulty="routine", intent="Step reading", db=db)
    follow = followups.add_follow_up(own.id, title="Follow", acceptance_criteria="Follow done", db=db)
    for node, reading, done in ((own, "Own reading", "Own done"), (step, "Run reading", "Step done"), (detached, "Run reading", "Detached done"), (follow, "Own reading", "Follow done")):
        shown = _node(db, "owner", node.id)
        assert (shown["intent"], shown["acceptance_criteria"]) == (reading, done)
    assert _node(db, "owner", step.id)["intent_run_title"] == "Run"
    db.close()


def test_worker_reads_delegated_parents_canonical_intent_and_names_owner():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    root = service.add_node(plan.id, kind="task", title="Root", acceptance_criteria="Root done", intent="Shared reading", db=db)
    parent = service.add_node(plan.id, kind="task", title="Delegated", acceptance_criteria="Parent done", intent_node_id=root.id, spawn_kind="claude-code", db=db)
    service.transition_node(parent.id, "active", db=db)
    mutations.attach_spawned_thread(parent.id, "worker", db=db)
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    step = service.add_node(worker_plan.id, kind="task", title="Worker step", acceptance_criteria="Worker done", intent="Worker's own reading", db=db)
    shown = _node(db, "worker", step.id)
    assert (shown["intent"], shown["acceptance_criteria"], shown["intent_parent_title"], shown["intent_owner_name"]) == ("Shared reading", "Worker done", "Delegated", "Plan owner")
    db.execute("UPDATE threads SET title=NULL WHERE id='owner'")
    assert _node(db, "worker", step.id)["intent_owner_name"] == "New Chat"
    db.close()


def test_nested_worker_names_the_owner_of_the_displayed_reading():
    db = _db()
    db.execute("INSERT INTO threads (id,user_id,project_id,spawned_by,title,created_at,updated_at) VALUES ('nested','user','project','worker','Nested','t','t')")
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = service.add_node(plan.id, kind="task", title="Original", acceptance_criteria="Done", intent="Shared reading", spawn_kind="claude-code", db=db)
    service.transition_node(parent.id, "active", db=db)
    mutations.attach_spawned_thread(parent.id, "worker", db=db)
    worker = service.ensure_plan("thread", "worker", db=db)
    child = service.add_node(worker.id, kind="task", title="Delegating child", acceptance_criteria="Done", intent_node_id=parent.id, spawn_kind="claude-code", db=db)
    service.transition_node(child.id, "active", db=db)
    mutations.attach_spawned_thread(child.id, "nested", db=db)
    nested = service.ensure_plan("thread", "nested", db=db)
    task = service.add_node(nested.id, kind="task", title="Nested task", acceptance_criteria="Done", db=db)
    shown = _node(db, "nested", task.id)
    assert (shown["intent"], shown["intent_parent_title"], shown["intent_owner_name"]) == ("Shared reading", "Delegating child", "Plan owner")
    db.close()


def test_settled_without_reading_preserves_own_done_test():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(plan.id, kind="task", title="Old", acceptance_criteria="Old done", db=db)
    service.transition_node(node.id, "active", db=db)
    service.transition_node(node.id, "closed", db=db)
    shown = _node(db, "owner", node.id)
    assert shown["intent"] is None and shown["acceptance_criteria"] == "Old done"
    db.close()


def test_legacy_feedback_label_is_derived_from_displayed_reading():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    for text, expected in (("User document feedback: original", True), ("Answer the comments", False)):
        node = service.add_node(plan.id, kind="task", title="Feedback", acceptance_criteria="Answered", intent=text, db=db)
        assert _node(db, "owner", node.id)["intent_is_person_comment"] is expected
        service.transition_node(node.id, "active", db=db)
        service.transition_node(node.id, "closed", db=db)
        assert _node(db, "owner", node.id)["intent_is_person_comment"] is expected
    db.close()


def test_worker_inherits_legacy_comment_label_and_own_done_test():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = service.add_node(plan.id, kind="task", title="Feedback", acceptance_criteria="Parent done", intent="User document feedback: exact words", spawn_kind="claude-code", db=db)
    service.transition_node(parent.id, "active", db=db)
    mutations.attach_spawned_thread(parent.id, "worker", db=db)
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    child = service.add_node(worker_plan.id, kind="task", title="Child", acceptance_criteria="Child done", intent="Different reading", db=db)
    shown = _node(db, "worker", child.id)
    assert shown["intent"] == "User document feedback: exact words"
    assert shown["intent_is_person_comment"] is True
    assert (shown["intent_parent_title"], shown["intent_owner_name"], shown["acceptance_criteria"]) == ("Feedback", "Plan owner", "Child done")
    db.close()


def test_reading_batch_uses_one_statement():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    nodes = [service.add_node(plan.id, kind="task", title=str(i), acceptance_criteria="Done", intent="Reading", db=db) for i in range(5)]
    statements = []
    db.set_trace_callback(statements.append)
    result = operator_intent.read([node.id for node in nodes], db=db)
    db.set_trace_callback(None)
    assert len(result) == 5 and len(statements) == 1
    db.close()
