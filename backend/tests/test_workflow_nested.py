"""Nested workflow contracts at validation, plan, and measurement boundaries."""

import subprocess
from types import SimpleNamespace

import pytest

from core.skill_validation import validate_skill_bundle
from core.workflow_verdict_read import _subtrees, run_roots
from tests.skill_sidecar_helpers import POINTER
from tests.test_workflow_task_fields import _mutate, db
from plans import service
from tools.workflow_task_fields import (
    Workflow, _missing_required_steps, _run_tasks_are_terminal, append_add_notice, append_close_notice,
    creation_version, with_referenced_version,
)


def _validate(value):
    skill = f"---\nname: outer\ndescription: Test\n{POINTER}---\n"
    return validate_skill_bundle(skill, {"corsia.yaml": f"workflow:\n  steps:\n    - id: design\n      workflow: {value}\n"})


def test_nested_workflow_validation():
    assert _validate("inner").ok
    for value in ('""', '123', 'outer'):
        assert not _validate(value).ok


def _phase(db, plan, title, ref, parent=None, step=None, version=None):
    return _mutate(db, plan.id, "add", {
        "kind": "phase", "title": title, "acceptance_criteria": "Recorded",
        "parent_id": parent, "workflow_ref": ref, "workflow_step": step,
        "difficulty": "standard" if step else None,
    }, workflow_version=version)["node"]


def test_nested_phase_counts_and_terminal(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    outer = _phase(db, plan, "Outer", "outer")
    inner = _phase(db, plan, "Inner", "inner", outer["id"], "design")
    phase = db.execute("SELECT * FROM plan_nodes WHERE id=?", (outer["id"],)).fetchone()
    workflows = {"outer": Workflow(("design",))}
    assert _missing_required_steps(db, phase, workflows) == ()
    assert not _run_tasks_are_terminal(db, outer["id"])
    closed = _mutate(db, plan.id, "close_node", {"node_id": inner["id"], "expected_revision": inner["revision"], "reason": "Finished"})["node"]
    assert _run_tasks_are_terminal(db, outer["id"])
    result = {}
    append_close_notice(result, node_id=inner["id"], db=db, workflows={"outer": Workflow(("design", "review"))})
    assert result["notice"] == "Required step review has no task in this run."
    _mutate(db, plan.id, "archive_node", {"node_id": inner["id"], "expected_revision": closed["revision"]})
    assert _missing_required_steps(db, phase, workflows) == ("design",)


def test_same_workflow_ancestor_notice(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    outer = _phase(db, plan, "Outer", "outer")
    middle = _phase(db, plan, "Middle", "inner", outer["id"], "design")
    inner = _phase(db, plan, "Repeat", "outer", middle["id"], "repeat")
    result = {"node": inner}
    append_add_notice(result, db, {"outer": Workflow((), version="a"), "inner": Workflow((), version="b")})
    assert "Workflow outer already runs above this one; a workflow inside itself repeats." in result["notice"]


def test_phase_dependencies_both_directions(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    outer = _phase(db, plan, "Outer", "outer")
    inner = _phase(db, plan, "Inner", "inner", outer["id"], "design")
    before = _mutate(db, plan.id, "add", {"title": "Before", "acceptance_criteria": "Done", "size": "S", "parent_id": outer["id"]})["node"]
    after = _mutate(db, plan.id, "add", {"title": "After", "acceptance_criteria": "Done", "size": "S", "parent_id": outer["id"]})["node"]
    _mutate(db, plan.id, "add_dependency", {"node_id": inner["id"], "depends_on_node_id": before["id"], "expected_revision": inner["revision"]})
    _mutate(db, plan.id, "add_dependency", {"node_id": after["id"], "depends_on_node_id": inner["id"], "expected_revision": after["revision"]})
    edges = {(row[0], row[1]) for row in db.execute("SELECT node_id,depends_on_node_id FROM plan_node_dependencies")}
    assert edges == {(inner["id"], before["id"]), (after["id"], inner["id"])}
    from plans import admission
    assert admission.project(inner["id"], db=db)["condition"] == "blocked"
    assert admission.project(after["id"], db=db)["condition"] == "blocked"


def test_nested_phase_dependency_blocks_inner_task_until_accepted(db):
    from plans import admission, node_waits
    from tests.plan_review_helpers import submit_review_evidence

    plan = service.ensure_plan("thread", "owner", db=db)
    outer = _phase(db, plan, "Outer", "outer")
    before = _mutate(db, plan.id, "add", {"title": "Before", "acceptance_criteria": "Done", "size": "S", "parent_id": outer["id"]})["node"]
    inner = _phase(db, plan, "Inner", "inner", outer["id"], "design")
    child = _mutate(db, plan.id, "add", {"title": "Inner step", "acceptance_criteria": "Done", "size": "S", "parent_id": inner["id"]})["node"]
    service.add_dependency(inner["id"], before["id"], db=db)
    rows = node_waits.unmet_prerequisites(db, [child["id"]])[child["id"]]
    assert [(r["id"], r["via"]) for r in rows] == [(before["id"], inner["id"])]
    assert admission.project(child["id"], db=db)["condition"] == "blocked"
    start = _mutate(db, plan.id, "start", {"node_id": child["id"], "expected_revision": child["revision"]})
    assert start["admission"]["condition"] == "blocked"
    assert start["admission"]["selection"] is None
    with pytest.raises(service.PlanInputError, match=inner["id"]):
        service.remove_dependency(child["id"], before["id"], db=db)
    service.transition_node(before["id"], "active", actor_id="owner", provenance="test", db=db)
    submit_review_evidence(before["id"], db=db, actor_id="owner")
    service.transition_node(before["id"], "accepted", actor_id="owner", provenance="test", verification_result="passed", verified_by="reviewer", db=db)
    assert admission.project(child["id"], db=db)["condition"] == "ready"
    with pytest.raises(service.PlanInputError, match=inner["id"]):
        service.remove_dependency(child["id"], before["id"], db=db)
    current = service.get_node(child["id"], db=db)
    started = _mutate(db, plan.id, "start", {"node_id": child["id"], "expected_revision": current.revision})
    assert started["admission"]["condition"] == "running"


def _task(db, plan, title, parent):
    return _mutate(db, plan.id, "add", {"title": title, "acceptance_criteria": "Done", "size": "S", "parent_id": parent})["node"]


def _events(db, node_id, event):
    return db.execute("SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND event=?", (node_id, event)).fetchone()[0]


def test_phase_dependency_on_its_own_step_is_not_inherited(db):
    from plans import admission, node_waits

    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan, "Outer", "outer")
    step = _task(db, plan, "Step", phase["id"])
    other = _task(db, plan, "Other", phase["id"])
    service.add_dependency(phase["id"], step["id"], db=db)
    assert node_waits.unmet_prerequisites(db, [step["id"], other["id"]]) == {}
    assert admission.project(step["id"], db=db)["condition"] == "ready"


def test_cycles_through_inheritance_or_containment_are_refused(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan, "Outer", "outer")
    step = _task(db, plan, "Step", phase["id"])
    outside = _task(db, plan, "Outside", None)
    service.add_dependency(phase["id"], outside["id"], db=db)
    with pytest.raises(service.PlanError, match="cycle"):
        service.add_dependency(outside["id"], step["id"], db=db)
    with pytest.raises(service.PlanError, match="cycle"):
        service.add_dependency(step["id"], phase["id"], db=db)
    later = _phase(db, plan, "Later", "outer")
    later_step = _task(db, plan, "Later step", later["id"])
    service.add_dependency(outside["id"], later_step["id"], db=db)
    with pytest.raises(service.PlanError, match="cycle"):
        service.add_dependency(later["id"], phase["id"], db=db)


def test_phase_dependency_records_blocked_on_its_steps(db):
    from tests.plan_review_helpers import submit_review_evidence

    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _phase(db, plan, "Outer", "outer")
    step = _task(db, plan, "Step", phase["id"])
    before = _task(db, plan, "Before", None)
    service.add_dependency(phase["id"], before["id"], db=db)
    assert _events(db, step["id"], "blocked_raised") == 1
    service.transition_node(before["id"], "active", actor_id="owner", provenance="test", db=db)
    submit_review_evidence(before["id"], db=db, actor_id="owner")
    service.transition_node(before["id"], "accepted", actor_id="owner", provenance="test", verification_result="passed", verified_by="reviewer", db=db)
    assert _events(db, step["id"], "blocked_cleared") == 1


def test_nested_versions_use_each_reference(db, monkeypatch, tmp_path):
    from tools import workflow_task_fields as fields

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    versions = {}
    workflows = {}
    for name in ("outer", "inner"):
        path = tmp_path / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: Workflow\n---\n", encoding="utf-8")
        subprocess.run(["git", "add", str(path.relative_to(tmp_path))], cwd=tmp_path, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", name], cwd=tmp_path, check=True)
        versions[name] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
        workflows[name] = Workflow((), skill=SimpleNamespace(scope="workspace", path=path))
    monkeypatch.setattr(fields.config, "WORKSPACE", tmp_path)
    plan = service.ensure_plan("thread", "owner", db=db)
    outer_version = creation_version({"workflow_ref": "outer"}, with_referenced_version({"workflow_ref": "outer"}, workflows))
    outer = _phase(db, plan, "Outer", "outer", version=outer_version)
    inner_version = creation_version({"workflow_ref": "inner"}, with_referenced_version({"workflow_ref": "inner"}, workflows))
    inner = _phase(db, plan, "Inner", "inner", outer["id"], "design", inner_version)
    assert outer["workflow_version"] == versions["outer"]
    assert inner["workflow_version"] == versions["inner"]


def test_nested_runs_are_distinct_measurement_subtrees():
    nodes = [
        {"id": "outer", "parent_id": None, "kind": "phase", "workflow_ref": "outer", "assignee": None},
        {"id": "outer-task", "parent_id": "outer", "kind": "task", "workflow_ref": "outer", "assignee": None},
        {"id": "inner", "parent_id": "outer", "kind": "phase", "workflow_ref": "inner", "assignee": None},
        {"id": "inner-task", "parent_id": "inner", "kind": "task", "workflow_ref": "inner", "assignee": None},
    ]
    roots = run_roots(nodes)
    assert {node["id"] for node in roots} == {"outer", "inner"}
    trees = _subtrees(nodes, (node["id"] for node in roots))
    assert [node["id"] for node in trees["outer"]] == ["outer-task"]
    assert [node["id"] for node in trees["inner"]] == ["inner-task"]
