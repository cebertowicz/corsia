"""Admission budgets the actual step-bound body."""
import pytest

from tests.skill_sidecar_helpers import POINTER
from tests.test_lesson_placement import CASE, _answer, _drain, _entry, _place, _skill, env


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [True, False])
async def test_admission_budgets_step_body_or_index(env, bound):
    folder = env.workspace / "skills" / "ship"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: ship\ndescription: Ship work\n{POINTER}---\n# Ship\n\n## Draft\nWrite it.\n"
    )
    (folder / "corsia.yaml").write_text(
        "handler: {kind: instruction}\nworkflow:\n  steps:\n"
        + ("    - {id: draft, title: Draft, required: true, check: Written, instruction: '## Draft'}\n"
           if bound else "    - {id: draft, title: Draft, required: true, check: Written}\n")
    )
    from dataclasses import replace
    from core import lesson_files

    original = lesson_files.read_trial
    def read_large(text):
        trial = original(text)
        return None if trial is None else replace(trial, body=" ".join(["guidance"] * 4000))
    env.monkeypatch.setattr(lesson_files, "read_trial", read_large)
    answer = _answer(case=CASE, cause="rule_gap", target_file="skills/ship/SKILL.md", target_section="Draft")
    reply = _place(env, _entry(env), answer)
    assert ("the trial exceeds the task-start guidance budget" in reply) is bound
    if not bound:
        await _drain(env.worker)
    assert _skill(env).exists() is not bound


@pytest.mark.asyncio
async def test_relative_target_binds_through_real_resolution(env):
    from core.skills_registry import trial_delivery_rows

    folder = env.workspace / "skills" / "ship"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: ship\ndescription: Ship work\n{POINTER}---\n# Ship\n\n## Draft\nWrite it.\n"
    )
    (folder / "corsia.yaml").write_text(
        "handler: {kind: instruction}\nworkflow:\n  steps:\n"
        "    - {id: draft, title: Draft, required: true, check: Written, instruction: '## Draft'}\n"
    )
    answer = _answer(case=CASE, cause="rule_gap", target_file="skills/ship/SKILL.md", target_section="Draft")
    _place(env, _entry(env), answer)
    await _drain(env.worker)
    assert _skill(env).exists()
    admitted, _ = trial_delivery_rows(thread_id="owner", project_id=None, workspace=env.workspace,
                                      db=env.db, workflow_ref="ship", workflow_step="draft")
    assert len(admitted) == 1
    assert admitted[0][1] == ("ship", frozenset({"draft"}))
    other, _ = trial_delivery_rows(thread_id="owner", project_id=None, workspace=env.workspace,
                                   db=env.db, workflow_ref="ship", workflow_step="other")
    assert not other

    # A more specific skill with the same name shadows the targeted file.
    shadow = env.tray / "skills" / "ship"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text("---\nname: ship\ndescription: Shadow\n---\n# Ship\n")
    admitted, _ = trial_delivery_rows(thread_id="owner", project_id=None, workspace=env.workspace, db=env.db)
    assert len(admitted) == 1 and admitted[0][1] is None
    from core.lesson_delivery import delivery_for_trial
    row, binding = admitted[0]
    assert delivery_for_trial(row.trial, row.path, row.source_data, binding).payload.startswith("- cite-the-head (trial, unproven):")
