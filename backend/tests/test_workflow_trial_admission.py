"""Admission budgets the actual step-bound body."""
import pytest

from tests.plan_writer_authorizer import raw_plan_seed
from tests.skill_sidecar_helpers import POINTER
from tests.test_lesson_placement import CASE, _answer, _drain, _entry, _place, _skill, env


def _ship(env, *, instruction: bool = True) -> None:
    folder = env.workspace / "skills" / "ship"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: ship\ndescription: Ship work\n{POINTER}---\n# Ship\n\n## Draft\nWrite it.\n"
    )
    (folder / "corsia.yaml").write_text(
        "handler: {kind: instruction}\nworkflow:\n  steps:\n"
        + ("    - {id: draft, title: Draft, required: true, check: Written, instruction: '## Draft'}\n"
           if instruction else "    - {id: draft, title: Draft, required: true, check: Written}\n")
    )


def _shadow(env) -> None:
    """A more specific skill with the same name shadows the targeted file for the owner thread."""
    shadow = env.tray / "skills" / "ship"
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text("---\nname: ship\ndescription: Shadow\n---\n# Ship\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(("bound", "shadowed"), [(True, False), (False, False), (True, True)])
async def test_admission_budgets_step_body_or_index(env, bound, shadowed):
    _ship(env, instruction=bound)
    if shadowed:
        # Other threads still deliver the step body, so the admitting thread's view doesn't decide.
        _shadow(env)
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


def _step(db, node: str, step: str) -> str:
    db.execute("PRAGMA foreign_keys=OFF")  # Only the node's workflow fields matter here.
    with raw_plan_seed(db):
        db.execute(
            "INSERT INTO plan_nodes (id,plan_id,order_key,kind,title,acceptance_criteria,state,commitment_id,"
            "requirements_revision,revision,created_at,updated_at,workflow_ref,workflow_step) "
            "VALUES (?, 'plan', 1, 'task', 't', 'c', 'pending', 'c', 1, 1, 't', 't', 'ship', ?)", (node, step))
    db.commit()
    return node


@pytest.mark.asyncio
async def test_relative_target_binds_through_real_resolution(env):
    from core.lesson_delivery import _resolve

    _ship(env)
    answer = _answer(case=CASE, cause="rule_gap", target_file="skills/ship/SKILL.md", target_section="Draft")
    _place(env, _entry(env), answer)
    await _drain(env.worker)
    assert _skill(env).exists()
    admitted, _ = _resolve(env.db, "owner", _step(env.db, "draft-node", "draft"))
    assert len(admitted) == 1 and "addition to this step's instruction" in admitted[0].payload
    other, _ = _resolve(env.db, "owner", _step(env.db, "other-node", "other"))
    assert not other

    _shadow(env)
    admitted, _ = _resolve(env.db, "owner", "draft-node")
    assert len(admitted) == 1 and admitted[0].payload.startswith("- cite-the-head (trial, unproven):")
