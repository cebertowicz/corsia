"""Approved versions are selected by plan relationships, not neighboring work."""

from pathlib import Path

from infra import config
from infra.database import get_db
from plans import service
from plans.approval_records import record_approval
from plans.approved_versions import approved_versions_for_node, approved_versions_for_pr
from registry import models
from tests.registry_helpers import make_session


def test_approval_and_child_are_tied_but_sibling_is_not(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    db = get_db()
    try:
        thread = make_session(db, prefix="approval-read")
        plan = service.ensure_plan("thread", thread, db=db)
        phase = service.add_node(plan.id, kind="phase", title="Phase", acceptance_criteria="Done", db=db)
        approval = service.add_node(plan.id, parent_id=phase.id, kind="task", title="Approve", acceptance_criteria="Done", db=db)
        child_phase = service.add_node(plan.id, parent_id=phase.id, kind="phase", title="Work", acceptance_criteria="Done", db=db)
        child = service.add_node(plan.id, parent_id=child_phase.id, kind="task", title="Child", acceptance_criteria="Done", db=db)
        sibling = service.add_node(plan.id, parent_id=phase.id, kind="task", title="Sibling", acceptance_criteria="Done", db=db)
        path = tmp_path / "design.md"
        path.write_text("design")
        artifact = models.register_artifact(integration="workspace", type="document", external_id=str(path), parent_thread_id=thread, title="Design", db=db)
        record_approval(db, artifact_id=artifact["id"], node_id=approval.id, source="bar", source_id="hold", doc_sha256="a" * 64, images=[])
        db.execute("INSERT INTO plan_node_dependencies (node_id,depends_on_node_id,created_at) VALUES (?,?,?)", (child_phase.id, approval.id, "now"))
        db.commit()
        assert len(approved_versions_for_node(approval.id, db=db)) == 1
        assert len(approved_versions_for_node(child.id, db=db)) == 1
        assert approved_versions_for_node(sibling.id, db=db) == []
        page = approved_versions_for_node(child.id, db=db)[0]["copies"][0]
        assert Path(page["copy_path"]) == tmp_path / ".corsia" / "approved" / ("a" * 16 + ".md")
    finally:
        db.close()


def test_follow_up_and_attached_worker_walk_to_approval(tmp_path, monkeypatch):
    from plans import followups, mutations
    from tests.test_operator_envelope_contract import _db

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    db = _db()
    try:
        db.execute("UPDATE threads SET project_id=NULL WHERE id IN ('owner','worker')")
        db.commit()
        plan = service.ensure_plan("thread", "owner", db=db)
        approval = service.add_node(plan.id, kind="task", title="Approval", acceptance_criteria="Done", db=db)
        first = followups.add_follow_up(approval.id, title="Follow", acceptance_criteria="Done", db=db)
        second = followups.add_follow_up(first.id, title="Follow again", acceptance_criteria="Done", db=db)
        path = tmp_path / "design.md"
        path.write_text("design")
        artifact = models.register_artifact(integration="workspace", type="document", external_id=str(path),
                                            parent_thread_id="owner", title="Design", db=db)
        record_approval(db, artifact_id=artifact["id"], node_id=approval.id, source="bar", source_id="hold",
                        doc_sha256="a" * 64, images=[])
        service.transition_node(second.id, "active", db=db)
        mutations.attach_spawned_thread(second.id, "worker", db=db)
        child_plan = service.ensure_plan("thread", "worker", db=db)
        child = service.add_node(child_plan.id, kind="task", title="Worker", acceptance_criteria="Done", db=db)
        assert len(approved_versions_for_node(second.id, db=db)) == 1
        assert len(approved_versions_for_node(child.id, db=db)) == 1
    finally:
        db.close()


def test_pr_lookup_preserves_all_link_outcomes_and_failures(isolated_db):
    import sqlite3
    from plans import closure
    from plans.public import link_node_artifact
    from tests.test_plan_linked_node_for_artifact import PR, _active_task, _plan, _registered_from

    db = get_db()
    try:
        thread, plan = _plan(db)
        task = _active_task(db, plan.id, "Open")
        artifact_id = _registered_from(db, thread, plan.id, task.id)
        assert approved_versions_for_pr(*PR, db=db)["tie_status"] == "none tied"
        other = _active_task(db, plan.id, "Other")
        link_node_artifact(other.id, artifact_id, "result", actor_id=None, provenance="test", db=db)
        db.commit()
        assert approved_versions_for_pr(*PR, db=db)["read_status"] == "several open tasks"
        closure.close_node(task.id, reason="Done", db=db)
        closure.close_node(other.id, reason="Done", db=db)
        db.commit()
        assert approved_versions_for_pr(*PR, db=db)["read_status"] == "no open task"
        assert approved_versions_for_pr("github", "pull_request", "missing", db=db)["read_status"] == "not registered"
        failed = sqlite3.connect(":memory:")
        failed.close()
        result = approved_versions_for_pr(*PR, db=failed)
        assert result["read_status"] == "lookup failed" and result["tie_status"] is None
    finally:
        db.close()


def test_unregistered_pr_is_distinct_from_no_ties(isolated_db):
    db = get_db()
    try:
        assert approved_versions_for_pr("github", "pull-request", "unknown", db=db)["read_status"] == "not registered"
    finally:
        db.close()
