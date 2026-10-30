"""Workflow-bound lessons participate only at the instructed step."""
from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

from core import skills_registry
from core.lesson_delivery import delivery_for_trial, draw, start_trials, step_bindings, trial_step_binding
from core.skill_scanning import scan_scope
from infra.schema import SCHEMA_SQL
from tests.test_lesson_delivery import _row
from tests.plan_writer_authorizer import raw_plan_seed
from tests.skill_sidecar_helpers import POINTER


@pytest.fixture
def trial_setup(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    folder = workspace / "skills" / "ship"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: ship\ndescription: Ship work\n{POINTER}---\n# Ship\n\n## Draft\nWrite it.\n\n## Review\nRead it.\n"
    )
    (folder / "corsia.yaml").write_text(
        "handler: {kind: instruction}\nworkflow:\n  steps:\n"
        "    - {id: draft, title: Draft, required: true, check: Written, instruction: '## Draft'}\n"
        "    - {id: review, title: Review, required: true, check: Read, instruction: '## Review'}\n"
    )
    monkeypatch.setattr("infra.config.WORKSPACE", workspace)
    monkeypatch.setattr("tools.sandbox_config._STATIC_ALLOWED_RW", [workspace.resolve(), tmp_path.resolve()])
    row = _row(tmp_path, 1, target_file="skills/ship/SKILL.md")
    lesson = replace(row.trial.lesson, case_metadata={"target_file": "skills/ship/SKILL.md", "target_section": "draft"})
    trial = replace(row.trial, lesson=lesson)
    row = replace(row, trial=trial)
    return workspace, row


def _resolution(monkeypatch, rows, workspace):
    resolved = SimpleNamespace(included=[*scan_scope(workspace, "workspace", "workspace"), *rows], omitted=[], precedence=(("thread", "thread"),))
    monkeypatch.setattr(skills_registry, "resolve_effective_skills", lambda **_: resolved)
    monkeypatch.setattr(skills_registry, "partition", lambda *_a, **_k: SimpleNamespace(per_task=rows))


def test_binding_normalizes_heading_and_unbound_skill_still_gets_index(trial_setup):
    workspace, row = trial_setup
    bindings = step_bindings(scan_scope(workspace, "workspace", "workspace"))
    binding = trial_step_binding(row.trial, bindings, workspace)
    assert binding == ("ship", frozenset({"draft"}))
    delivery = delivery_for_trial(row.trial, row.path, row.source_data, binding)
    assert delivery.section is None
    assert "addition to this step's instruction" in delivery.payload and "Check Check" in delivery.payload
    other = replace(row.trial, lesson=replace(row.trial.lesson, case_metadata={
        "target_file": "skills/ship/SKILL.md", "target_section": "Unrelated"}))
    assert trial_step_binding(other, bindings, workspace) is None
    assert delivery_for_trial(other, row.path, row.source_data).payload.startswith("- trial-01 (trial, unproven):")
    skill = scan_scope(workspace, "workspace", "workspace")[0]
    assert not step_bindings([replace(skill, source_data=b"# Missing heading\n")])


def test_shadowed_target_remains_unbound(trial_setup, tmp_path, monkeypatch):
    from core.skill_scanning import resolve_winners

    workspace, row = trial_setup
    higher = tmp_path / "thread"
    folder = higher / "skills" / "ship"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: ship\ndescription: Thread skill\n---\n# Ship\n")
    included, omitted = resolve_winners(scan_scope(workspace, "workspace", "workspace") +
                                         scan_scope(higher, "thread", "thread"))
    assert len(omitted) == 1
    resolved = SimpleNamespace(included=included, omitted=omitted, precedence=(("thread", "thread"),))
    monkeypatch.setattr(skills_registry, "resolve_effective_skills", lambda **_: resolved)
    monkeypatch.setattr(skills_registry, "partition", lambda *_a, **_k: SimpleNamespace(per_task=[row]))
    admitted, _ = skills_registry.trial_delivery_rows(thread_id="thread", project_id=None, workspace=workspace, db=None)
    assert admitted == [(row, None)]
    assert delivery_for_trial(row.trial, row.path, row.source_data, admitted[0][1]).payload.startswith("- trial-01 (trial, unproven):")


def test_delivery_uses_resolution_snapshot_without_reading_skill(trial_setup, monkeypatch):
    workspace, row = trial_setup
    _resolution(monkeypatch, [row], workspace)
    monkeypatch.setattr("core.skill_validation.validate_skill_file", lambda *_: pytest.fail("unexpected read"))
    admitted, _ = skills_registry.trial_delivery_rows(thread_id="thread", project_id=None, workspace=workspace,
                                                       db=None, workflow_ref="ship", workflow_step="draft")
    assert "Check Check" in delivery_for_trial(row.trial, row.path, row.source_data, admitted[0][1]).payload


def test_filter_precedes_count_cap(trial_setup, monkeypatch, tmp_path):
    workspace, bound = trial_setup
    rows = [bound] + [_row(tmp_path, n) for n in range(2, 43)]
    _resolution(monkeypatch, rows, workspace)
    admitted, omitted = skills_registry.trial_delivery_rows(
        thread_id="thread", project_id=None, workspace=workspace, db=None,
        workflow_ref="ship", workflow_step="review")
    assert bound not in [row for row, _ in admitted + omitted]
    assert len(admitted) == 40 and len(omitted) == 1


def test_bound_receipts_and_both_arms(trial_setup, monkeypatch):
    workspace, row = trial_setup
    _resolution(monkeypatch, [row], workspace)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    try:
        lesson = row.trial.lesson.id
        sha = row.trial.behavioral_sha256
        nodes = {v: next(f"node-{v}-{i}" for i in range(1000) if draw(lesson, sha, f"node-{v}-{i}") == v)
                 for v in ("with", "without")}
        with raw_plan_seed(db):
            for node, step in [("unrelated", "review"), (nodes["with"], "draft"), (nodes["without"], "draft")]:
                db.execute("INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,created_at,updated_at,workflow_ref,workflow_step) VALUES (?, 'plan', 1, 'task', 't', 'c', 'pending', 'c', 1, 1, 't', 't', 'ship', ?)", (node, step))
        db.commit()
        assert start_trials(db, thread_id="unrelated-thread", node_id="unrelated") == []
        assert db.execute("SELECT count(*) FROM guidance_loads WHERE node_id='unrelated'").fetchone()[0] == 0
        assert start_trials(db, thread_id="without-thread", node_id=nodes["without"]) == []
        result = start_trials(db, thread_id="with-thread", node_id=nodes["with"])
        assert len(result) == 1 and "Check Check" in result[0]["guidance"]
        assert {r[0] for r in db.execute("SELECT variant FROM guidance_loads")} == {"with", "without"}
    finally:
        db.close()
