"""Admission budgets the actual step-bound body."""
from tests.skill_sidecar_helpers import POINTER
from tests.test_lesson_placement import CASE, _answer, _entry, _place, _skill, env


def test_large_step_trial_is_refused_at_admission(env):
    folder = env.workspace / "skills" / "ship"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: ship\ndescription: Ship work\n{POINTER}---\n# Ship\n\n## Draft\nWrite it.\n"
    )
    (folder / "corsia.yaml").write_text(
        "handler: {kind: instruction}\nworkflow:\n  steps:\n"
        "    - {id: draft, title: Draft, required: true, check: Written, instruction: '## Draft'}\n"
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
    assert "the trial exceeds the task-start guidance budget" in reply
    assert not _skill(env).exists()
