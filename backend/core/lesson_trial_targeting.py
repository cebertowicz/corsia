"""Trial target validation and matching for lesson admission."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core import lesson_files
from core.placement_decide import Decision, InvalidDecision, ThreadPlace


class TrialRefused(InvalidDecision):
    """The trial cannot be placed as answered; ``code`` is the worker's reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def target(decision: Decision) -> str:
    """Return the normalized target after rejecting system projections."""
    from infra.config import WORKSPACE
    from tools.filesystem_gate import canonical_target_path

    raw = decision.case_metadata.get("new_target_path") or decision.case_metadata.get("target_file")
    value = canonical_target_path(str(raw), workspace=WORKSPACE)
    if value == ".corsia" or value.startswith(".corsia/"):
        raise TrialRefused("invalid-learning-metadata", "System projections cannot be lesson targets.")
    return value


def additive_only(decision: Decision) -> None:
    """Refuse an in-context trial whose target already has its section."""
    from core.skill_validation import is_skill_file
    from infra.config import WORKSPACE
    from tools.filesystem_gate import resolve_path

    metadata = decision.case_metadata
    target_file = metadata.get("target_file")
    if metadata.get("new_target_path") or not target_file or is_skill_file(Path(str(target_file))):
        return
    normalized = target(decision)
    if Path(normalized).is_absolute():
        raise TrialRefused("non-additive", (
            f"{target_file} is outside the workspace; a trial for a file already in "
            "context must target a workspace file"
        ))
    try:
        text = resolve_path(normalized, workspace=Path(WORKSPACE)).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise TrialRefused("non-additive", (
            f"{target_file} could not be read, so a trial cannot show it only adds a section"
        )) from None
    if lesson_files.has_section(text, str(metadata.get("target_section") or "")):
        raise TrialRefused("non-additive", (
            f"{target_file} is already in context and has a '{metadata.get('target_section')}' "
            "section; a trial there may only add a new section, so name a new target_section "
            "or place the change without trial"
        ))


def target_trials(
    place: ThreadPlace, decision: Decision,
) -> tuple[list[tuple[Path, Any]], list[tuple[Path, Any]]]:
    """Return this owner's open and retired trials for the decision's target."""
    if place.tray is None:
        return [], []
    open_trials, retired = [], []
    normalized = target(decision)
    for path in sorted((place.tray / "skills").glob("*/SKILL.md")):
        try:
            current = lesson_files.read_trial(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if (
            current is None
            or current.lesson.owner != place.thread_id
            or target_from_lesson(current) != normalized
        ):
            continue
        if current.lesson.state in ("watch", "trial"):
            open_trials.append((path, current))
        elif current.lesson.state in ("retired", "promoted"):
            retired.append((path, current))
    return open_trials, retired


def same_case(current: Any, decision: Decision) -> bool:
    """Whether an existing trial has the same section and correction cause."""
    current_meta, requested = current.lesson.case_metadata, decision.case_metadata
    return (current_meta.get("target_section") == requested.get("target_section")
            and current_meta.get("cause") == requested.get("cause"))


def target_from_lesson(current: Any) -> str | None:
    """Return an existing trial's normalized target, if it has one."""
    from infra.config import WORKSPACE
    from tools.filesystem_gate import canonical_target_path

    metadata = current.lesson.case_metadata
    raw = metadata.get("new_target_path") or metadata.get("target_file")
    return canonical_target_path(str(raw), workspace=WORKSPACE) if raw else None
