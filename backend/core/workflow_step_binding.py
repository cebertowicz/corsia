"""Which workflow steps a trial lesson runs at: the one owner of that decision.

A trial is bound when its target is a workflow skill's ``SKILL.md`` and its
target section is the instruction the run builder copies for a step. Admission
and task-start delivery both ask here, so they budget and deliver one payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.lesson_files import TrialFile, section_key
from core.skill_registry_types import SkillRow
from core.skill_validation import SkillValidation, instruction_section, is_skill_file, parse_frontmatter


@dataclass(frozen=True)
class StepBinding:
    """The workflow steps whose instruction is the section a trial targets; it runs only at them."""

    workflow: str
    steps: frozenset[str]


def step_binding(skill: SkillRow | SkillValidation | None, heading: str) -> StepBinding | None:
    """The steps of ``skill``'s workflow whose instruction the run builder copies from ``heading``.

    It uses the run builder's own section parser on the same body, so a step
    is bound only when its run would carry that instruction.
    """
    workflow = None if skill is None or skill.corsia is None else skill.corsia.get("workflow")
    steps = workflow.get("steps") if isinstance(workflow, dict) else None
    if skill is None or not isinstance(steps, list) or skill.source_data is None or not skill.name:
        return None
    body = parse_frontmatter(skill.source_data.decode("utf-8"))[1]
    ids = frozenset(
        step["id"] for step in steps
        if isinstance(step, dict) and isinstance(step.get("id"), str) and isinstance(step.get("instruction"), str)
        and section_key(step["instruction"]) == section_key(heading)
        and instruction_section(body, step["instruction"]) is not None
    )
    return StepBinding(skill.name, ids) if ids else None


def skill_target(trial: TrialFile, workspace: Path) -> tuple[Path, str] | None:
    """The canonical in-workspace SKILL.md and section a trial targets; None otherwise.

    Path identity only, with no authorization step: task start runs this for
    every trial row and must not reach repository policy (SQLite, Git).
    """
    from tools.filesystem_gate import join_and_resolve

    metadata = trial.lesson.case_metadata
    target, heading = metadata.get("target_file"), metadata.get("target_section")
    if not isinstance(target, str) or not isinstance(heading, str) or not is_skill_file(Path(target)):
        return None
    try:
        path = join_and_resolve(target, workspace=workspace)
    except (OSError, ValueError):
        return None
    return (path, heading) if path.is_relative_to(workspace.expanduser().resolve()) else None


def declared_binding(
    trial: TrialFile, skills: dict[Path, SkillRow],
) -> tuple[StepBinding | None, bool]:
    """The steps the trial's target file itself binds, and whether that file is in ``skills``.

    The binding is the target file's, never one thread's view of it, so a trial
    version has one intervention everywhere. ``skills`` is the caller's scan
    snapshot by path; a target outside it is read once.
    """
    from core import skill_validation
    from infra.config import WORKSPACE

    target = skill_target(trial, Path(WORKSPACE))
    if target is None:
        return None, False
    path, heading = target
    skill = skills.get(path) or skill_validation.validate_skill_file(path)
    return step_binding(skill, heading), path in skills
