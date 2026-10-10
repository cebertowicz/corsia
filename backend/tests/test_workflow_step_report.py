"""Focused coverage for the read-only workflow-step report."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from infra.schema import SCHEMA_SQL
from plans import mutations, service

# Load the script by path: putting scripts/ on sys.path would let scripts/tests
# shadow this suite's own `tests` package for every module collected after it.
_SPEC = importlib.util.spec_from_file_location(
    "workflow_step_report", Path(__file__).resolve().parents[2] / "scripts" / "workflow_step_report.py",
)
assert _SPEC is not None and _SPEC.loader is not None
workflow_step_report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(workflow_step_report)
GAP, main, report = workflow_step_report.GAP, workflow_step_report.main, workflow_step_report.report
_compare, _text = workflow_step_report._compare, workflow_step_report._text


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','P','t','t')")
    connection.execute("""INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
                          VALUES ('owner','u','p','test/model','t','t')""")
    yield connection
    connection.close()


def _node(db, plan_id, title, **fields):
    return service.add_node(plan_id, kind=fields.pop("kind", "task"), title=title,
                            acceptance_criteria="done", db=db, **fields)


def _usage(db, node, usage_id, model, tokens, cost, role="execution"):
    db.execute(
        """INSERT INTO llm_usage (id,thread_id,turn_id,model,total_tokens,cost_usd,plan_id,
           plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role,source,created_at)
           VALUES (?, 'owner', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 't')""",
        (usage_id, model, tokens, cost, node.plan_id, node.id, f"a-{usage_id}",
         node.commitment_id, role, "test"),
    )
    db.commit()


def _attempt(db, node, ordinal):
    executor = f"executor-{node.id}-{ordinal}"
    db.execute("INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at) VALUES (?, 'u', 'p', 'test/model', 't', 't')", (executor,))
    db.execute("""INSERT INTO plan_node_attempts (id,plan_id,node_id,executor_thread_id,requirements_revision,
                  attempt_ordinal,selected_by_actor_id,provenance,selected_at)
                  VALUES (?,?,?,?,1,1,?,'test','t')""",
               (f"attempt-{node.id}-{ordinal}", node.plan_id, node.id, executor, "owner"))


def _revision(db, parent, title, **fields):
    revision = _node(db, parent.plan_id, title, parent_id=parent.parent_id, **fields)
    db.execute("UPDATE plan_nodes SET follow_up_of=? WHERE id=?", (parent.id, revision.id))
    service._event(db, revision.id, "revision_created", actor_id="owner", provenance="test",
                   payload={"source_node_id": parent.id})
    return revision


def test_two_models_split_and_required_gap(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, plan.id, "run", kind="phase", workflow_ref="demo", workflow_version="v1")
    task = _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
                 workflow_version="v1", workflow_step="build", difficulty="standard")
    _usage(db, task, "u1", "model/a", 10, 0.1)
    _usage(db, task, "u2", "model/b", 20, 0.2, "correction")

    result = report(db)

    assert result["header"]["required_step_gap"] == GAP
    group = result["groups"][0]
    assert group["run_count"] == 1
    assert {row["model"] for row in group["models"]} == {"model/a", "model/b"}
    assert next(row for row in group["models"] if row["model"] == "model/b")["correction_token_share"] == 1
    assert group["metrics"]["attribution_mismatch_nodes"] == []
    with pytest.raises(ValueError, match="exactly two"):
        _compare(result, "demo")


def test_a_fewer_than_six_runs_are_counts_only(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, plan.id, "run", kind="phase", workflow_ref="demo", workflow_version="v1")
    task = _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
                 workflow_version="v1", workflow_step="build", difficulty="standard")
    _usage(db, task, "u1", "model/a", 10, 0.1)

    metrics = report(db)["groups"][0]["metrics"]
    assert metrics["tokens"]["counts_only"] is True
    assert "median" not in metrics["tokens"]


def test_b_run_skip_reason_is_reported(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, plan.id, "run", kind="phase", workflow_ref="demo", workflow_version="v1",
                  description="skipped: review, not applicable to this run")
    _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
          workflow_version="v1", workflow_step="build", difficulty="standard")

    assert report(db)["runs"][0]["skipped"] == ["review, not applicable to this run"]


def test_rollup_mismatch_is_visible_and_sibling_absence(db, monkeypatch):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase_one = _node(db, plan.id, "run one", kind="phase", workflow_ref="demo", workflow_version="v1")
    parent = _node(db, plan.id, "build", parent_id=phase_one.id, workflow_ref="demo",
                   workflow_version="v1", workflow_step="build", difficulty="standard")
    # Tasks are leaves; model a rollup mismatch below without mutating plan structure.
    phase_two = _node(db, plan.id, "run two", kind="phase", workflow_ref="demo", workflow_version="v1",
                      description="skipped: review, not applicable")
    _node(db, plan.id, "review", parent_id=phase_two.id, workflow_ref="demo",
          workflow_version="v1", workflow_step="review", difficulty="routine")
    from plans import node_cost

    original = node_cost.project_many

    def inflated_rollup(*args, **kwargs):
        values = original(*args, **kwargs)
        values[plan.id][parent.id]["own"]["tokens"]["known"] = 10
        values[plan.id][parent.id]["own"]["model_cost_usd"]["known"] = 0.1
        return values

    monkeypatch.setattr(node_cost, "project_many", inflated_rollup)
    result = report(db)

    build = next(row for row in result["groups"] if row["step"] == "build")
    assert build["metrics"]["attribution_mismatch_nodes"] == [parent.id]
    first_run = next(run for run in result["runs"] if run["run_id"] == phase_one.id)
    assert "review" in first_run["absent_vs_sibling_runs"]


def test_six_runs_have_spread_and_compare_has_two_versions(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    for ordinal in range(6):
        phase = _node(db, plan.id, f"run {ordinal}", kind="phase", workflow_ref="demo", workflow_version="v1")
        task = _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
                     workflow_version="v1", workflow_step="build", difficulty="standard")
        _usage(db, task, f"v1-{ordinal}", "model/a", ordinal + 1, 0.1)
    phase = _node(db, plan.id, "v2", kind="phase", workflow_ref="demo", workflow_version="v2",
                  description="skipped: review, not applicable")
    task = _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
                 workflow_version="v2", workflow_step="build", difficulty="standard")
    _node(db, plan.id, "added", parent_id=phase.id, workflow_ref="demo",
          workflow_version="v2", workflow_step="+added", difficulty="routine")
    _usage(db, task, "v2", "model/b", 10, 1.0)

    result = report(db, "demo")
    v1 = next(group for group in result["groups"] if group["workflow_version"] == "v1")
    assert v1["metrics"]["tokens"]["n"] == 6
    assert "median" in v1["metrics"]["tokens"]
    assert any(run["added_steps"] == ["+added"] for run in result["runs"])
    comparison = _compare(result, "demo")
    assert comparison["versions"] == ["v1", "v2"]


def test_null_and_recorded_versions_and_ratings_sort_and_render(db):
    """A recorded-null version or rating beside a string one must not crash any output."""
    plan = service.ensure_plan("thread", "owner", db=db)
    for commit, difficulty in ((None, None), ("v2", "standard"), (None, "standard"), ("v2", None)):
        phase = _node(db, plan.id, "run", kind="phase", workflow_ref="demo", workflow_version=commit)
        task = _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
                     workflow_version=commit, workflow_step="build", difficulty="standard")
        # Creation now requires a rating; older rows can still hold none.
        db.execute("UPDATE plan_nodes SET difficulty=? WHERE id=?", (difficulty, task.id))

    result = report(db, "demo")
    result["compare"] = _compare(result, "demo")

    assert [(group["workflow_version"], group["difficulty"]) for group in result["groups"]] == [
        (None, None), (None, "standard"), ("v2", None), ("v2", "standard"),
    ]
    assert result["compare"]["versions"] == [None, "v2"]
    assert [sorted(step["by_version"]) for step in result["compare"]["steps"]] == [["null", "v2"]] * 2
    assert "compare demo: versions=[None, 'v2']" in _text(result)
    assert json.loads(json.dumps(result, sort_keys=True, default=str))["compare"]["versions"] == [None, "v2"]


def test_j_rejected_revision_is_one_chain_with_rework(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, plan.id, "run", kind="phase", workflow_ref="demo", workflow_version="v1")
    first = _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
                  workflow_version="v1", workflow_step="build", difficulty="standard")
    revision = _revision(db, first, "build revised")
    _attempt(db, first, 1)
    _attempt(db, revision, 1)
    _usage(db, first, "first", "model/a", 10, 0.1)
    _usage(db, revision, "revision", "model/a", 20, 0.2)
    db.execute("UPDATE plan_nodes SET state='failed' WHERE id=?", (first.id,))
    db.execute("UPDATE plan_nodes SET state='accepted' WHERE id=?", (revision.id,))
    db.commit()

    group = report(db)["groups"][0]
    assert group["metrics"]["task_count"] == 1
    assert group["metrics"]["attempts_total"] == 2
    assert group["metrics"]["attempts"]["counts_only"] is True
    assert group["metrics"]["revisions"] == 1
    assert group["metrics"]["first_attempt_accepted"] == 0
    assert group["metrics"]["failed_or_closed_without_completion"] == 0
    assert group["metrics"]["tokens_known_total"] == 30
    assert group["metrics"]["tokens"]["counts_only"] is True
    assert group["metrics"]["rework_tokens_known_total"] == 20


def test_k_revision_without_workflow_fields_lands_in_root_step(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, plan.id, "run", kind="phase", workflow_ref="demo", workflow_version="v1")
    first = _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
                  workflow_version="v1", workflow_step="build", difficulty="standard")
    revision = _revision(db, first, "build revised")
    _usage(db, revision, "revision", "model/a", 20, 0.2)

    group = report(db)["groups"][0]
    assert group["step"] == "build"
    assert group["metrics"]["task_count"] == 1


def test_l_accept_with_follow_up_is_a_separate_step(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, plan.id, "run", kind="phase", workflow_ref="demo", workflow_version="v1")
    original = _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
                     workflow_version="v1", workflow_step="build", difficulty="standard")
    follow_up = _node(db, plan.id, "review", parent_id=phase.id, workflow_ref="demo",
                      workflow_version="v1", workflow_step="review", difficulty="standard")
    db.execute("UPDATE plan_nodes SET state='accepted_with_followup' WHERE id=?", (original.id,))
    db.execute("UPDATE plan_nodes SET follow_up_of=? WHERE id=?", (original.id, follow_up.id))
    _attempt(db, original, 1)
    _attempt(db, follow_up, 1)
    db.commit()

    groups = {group["step"]: group for group in report(db)["groups"]}
    assert set(groups) == {"build", "review"}
    assert groups["build"]["metrics"]["first_attempt_accepted"] == 1


def test_attached_child_usage_does_not_create_own_attribution_mismatch(db):
    db.execute("INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at) VALUES ('child', 'u', 'p', 'owner', 'test/model', 't', 't')")
    parent_plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, parent_plan.id, "run", kind="phase", workflow_ref="demo", workflow_version="v1")
    step = _node(db, parent_plan.id, "delegate", parent_id=phase.id, spawn_kind="claude-code",
                 workflow_ref="demo", workflow_version="v1", workflow_step="delegate", difficulty="standard")
    service.transition_node(step.id, "active", db=db)
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    child_plan = db.execute("SELECT id FROM plans WHERE owner_id='child'").fetchone()[0]
    child_task = _node(db, child_plan, "child work")
    _usage(db, child_task, "child-usage", "model/a", 10, 0.1)

    result = report(db)

    group = next(group for group in result["groups"] if group["step"] == "delegate")
    assert group["metrics"]["attribution_mismatch_nodes"] == []


def test_unknown_usage_and_closed_node_are_reported(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = _node(db, plan.id, "run", kind="phase", workflow_ref="demo", workflow_version="v1")
    task = _node(db, plan.id, "build", parent_id=phase.id, workflow_ref="demo",
                 workflow_version="v1", workflow_step="build", difficulty="standard")
    _usage(db, task, "unknown", "model/a", None, None)
    db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (task.id,))
    db.commit()

    group = report(db)["groups"][0]["metrics"]
    assert group["tokens_unknown_rows"] == 1 and group["cost_unknown_rows"] == 1
    assert group["failed_or_closed_without_completion"] == 1


def test_main_json_has_export_shape(tmp_path, capsys):
    path = tmp_path / "report.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.close()

    assert main(["--db", str(path), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {"header", "groups", "runs"} <= result.keys()
