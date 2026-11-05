"""Workflow-bound lessons participate only at the instructed step."""
from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

from core import skills_registry
from core.lesson_delivery import _resolve, delivery_for_trial, draw, start_trials
from core.workflow_step_binding import StepBinding, skill_target, step_binding
from core.skill_scanning import scan_scope
from core.skill_validation import instruction_section, parse_frontmatter
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


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    yield connection
    connection.close()


def _resolution(monkeypatch, rows, workspace, included=None):
    included = [*scan_scope(workspace, "workspace", "workspace"), *rows] if included is None else included
    resolved = SimpleNamespace(included=included, omitted=[], precedence=(("thread", "thread"),))
    monkeypatch.setattr(skills_registry, "resolve_effective_skills", lambda **_: resolved)
    monkeypatch.setattr(skills_registry, "partition", lambda *_a, **_k: SimpleNamespace(per_task=rows))


def _step(db, node: str, step: str) -> str:
    with raw_plan_seed(db):
        db.execute(
            "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,"
            "requirements_revision,revision,created_at,updated_at,workflow_ref,workflow_step) "
            "VALUES (?, 'plan', 1, 'task', 't', 'c', 'pending', 'c', 1, 1, 't', 't', 'ship', ?)", (node, step))
    db.commit()
    return node


def _ship(workspace, body: str, frontmatter: str = ""):
    """Rewrite the ship skill's SKILL.md with ``body``; its draft step keeps instruction '## Draft'."""
    (workspace / "skills" / "ship" / "SKILL.md").write_text(
        f"---\nname: ship\ndescription: Ship work\n{frontmatter}{POINTER}---\n{body}"
    )
    return scan_scope(workspace, "workspace", "workspace")[0]


def test_binding_normalizes_heading_and_unbound_skill_still_gets_index(trial_setup):
    workspace, row = trial_setup
    skill = scan_scope(workspace, "workspace", "workspace")[0]
    path, heading = skill_target(row.trial, workspace)
    assert path == skill.path
    binding = step_binding(skill, heading)
    assert binding == StepBinding("ship", frozenset({"draft"}))
    delivery = delivery_for_trial(row.trial, row.path, row.source_data, binding)
    assert delivery.section is None
    assert "addition to this step's instruction" in delivery.payload and "Check Check" in delivery.payload
    assert step_binding(skill, "Unrelated") is None
    assert delivery_for_trial(row.trial, row.path, row.source_data).payload.startswith("- trial-01 (trial, unproven):")
    assert step_binding(replace(skill, source_data=b"# Missing heading\n"), heading) is None
    outside = replace(row.trial.lesson, case_metadata={"target_file": "/elsewhere/SKILL.md", "target_section": "draft"})
    assert skill_target(replace(row.trial, lesson=outside), workspace) is None


@pytest.mark.parametrize(("body", "frontmatter"), [
    ("# Ship\n\n### Draft\nWrite it.\n", ""),          # another level, which the run builder rejects
    ("# Ship\n\n```\n## Draft\n```\n", ""),             # only inside a code fence
    ("# Ship\n", "## Draft\n"),                         # only in the frontmatter (a YAML comment)
])
def test_binding_follows_the_run_builders_section_parser(trial_setup, body, frontmatter):
    workspace, _ = trial_setup
    skill = _ship(workspace, body, frontmatter)
    assert instruction_section(parse_frontmatter(skill.source_data.decode())[1], "## Draft") is None
    assert step_binding(skill, "Draft") is None


def test_shadowed_target_keeps_its_binding_and_is_omitted_at_its_step(trial_setup, tmp_path, monkeypatch, db):
    from core.skill_scanning import resolve_winners

    workspace, row = trial_setup
    higher = tmp_path / "thread"
    folder = higher / "skills" / "ship"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: ship\ndescription: Thread skill\n---\n# Ship\n")
    included, omitted = resolve_winners(scan_scope(workspace, "workspace", "workspace") +
                                         scan_scope(higher, "thread", "thread"))
    assert len(omitted) == 1
    _resolution(monkeypatch, [row], workspace, included=included)
    # One trial version, one intervention: never the index line where another file shadows the target.
    assert _resolve(db, "thread", _step(db, "node", "review")) == ([], [])
    admitted, omitted = _resolve(db, "thread", _step(db, "draft-node", "draft"))
    assert admitted == [] and [trial.omission_reason for trial in omitted] == ["target_not_in_context"]
    assert "addition to this step's instruction" in omitted[0].payload


def test_delivery_uses_resolution_snapshot_without_reading_skill(trial_setup, monkeypatch, db):
    workspace, row = trial_setup
    _resolution(monkeypatch, [row], workspace)
    monkeypatch.setattr("core.skill_validation.validate_skill_file", lambda *_: pytest.fail("unexpected read"))
    # Path identity only: repository policy (SQLite, Git) stays off the task-start path.
    monkeypatch.setattr("tools.filesystem_gate.resolve_path", lambda *_a, **_k: pytest.fail("authorization"))
    admitted, _ = _resolve(db, "thread", _step(db, "node", "draft"))
    assert "Check Check" in admitted[0].payload


def test_filter_precedes_count_cap(trial_setup, monkeypatch, tmp_path, db):
    workspace, bound = trial_setup
    rows = [bound] + [_row(tmp_path, n) for n in range(2, 43)]
    _resolution(monkeypatch, rows, workspace)
    admitted, omitted = _resolve(db, "thread", _step(db, "node", "review"))
    assert bound.trial.lesson.id not in {trial.lesson_id for trial in admitted + omitted}
    assert [trial.omission_reason for trial in omitted].count("count_cap") == 1


def test_bound_receipts_and_both_arms(trial_setup, monkeypatch, db):
    workspace, row = trial_setup
    _resolution(monkeypatch, [row], workspace)
    lesson = row.trial.lesson.id
    sha = row.trial.behavioral_sha256
    nodes = {v: next(f"node-{v}-{i}" for i in range(1000) if draw(lesson, sha, f"node-{v}-{i}") == v)
             for v in ("with", "without")}
    _step(db, "unrelated", "review")
    _step(db, nodes["with"], "draft")
    _step(db, nodes["without"], "draft")
    assert start_trials(db, thread_id="unrelated-thread", node_id="unrelated") == []
    assert db.execute("SELECT count(*) FROM guidance_loads WHERE node_id='unrelated'").fetchone()[0] == 0
    assert start_trials(db, thread_id="without-thread", node_id=nodes["without"]) == []
    result = start_trials(db, thread_id="with-thread", node_id=nodes["with"])
    assert len(result) == 1 and "Check Check" in result[0]["guidance"]
    assert {r[0] for r in db.execute("SELECT variant FROM guidance_loads")} == {"with", "without"}
