"""Proof that a lesson is where a thread says it is, before its entry retires.

Retiring a continuity entry because its lesson "is already at X" or "was
written to X" is destructive: once retired, nothing re-reviews it. So an
agent's in-turn ``place`` and the checkpoint fork's ``already`` verdict both
name the line that carries the lesson, and CORSIA reads the destination and
finds that line before anything retires (#2479), with the same
``placement_decide.says`` test the worker applies to a duplicate or a fold.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from core.placement_decide import MIN_QUOTE, says, squash
from infra.continuity_store import ContinuityError


class NotThere(ContinuityError):
    """The destination does not verifiably carry the lesson; says what to fix."""


def verify(
    db: sqlite3.Connection, thread_id: str, destination: str, quote: str
) -> str:
    """The destination's workspace-relative path once it is shown to say ``quote``.

    ``destination`` is a file path: absolute, or relative to the thread's own
    scope (its project directory, as the placement table names paths) or to
    the workspace. It must be a surface this thread is delivered (see
    ``_delivered``): a line in scratch or an upload reaches no later turn.
    Raises ``NotThere`` naming what the caller must correct.
    """
    from infra.config import WORKSPACE

    workspace = Path(WORKSPACE).resolve()
    if len(squash(str(quote or "").lstrip("-* "))) < MIN_QUOTE:
        raise NotThere(
            f"quote must copy at least {MIN_QUOTE} characters of the line in "
            "the destination that carries the lesson"
        )
    path = _resolve(db, thread_id, str(destination).strip(), workspace)
    if path is None:
        raise NotThere(
            f"destination {destination!r} is not a file in the workspace; name "
            "the file that carries the lesson (a proposal is verdict proposed)"
        )
    skills = offered_skills(db, thread_id, workspace)
    held = _held_trial(skills, path)
    if held is not None:
        raise NotThere(
            f"{path.relative_to(workspace)} is a {held}. A trial lesson is "
            "offered only once its review exists: keep the entry and place it "
            "with verdict trial, which writes the file and opens the review"
        )
    if not _delivered(db, thread_id, path, workspace, skills):
        raise NotThere(
            f"{path.relative_to(workspace)} is not delivered to this thread; "
            "name its bootloader, a context file or skill it loads, or its "
            "agent reference"
        )
    if not says(path.read_text(errors="replace"), quote):
        raise NotThere(
            f"{path.relative_to(workspace)} does not contain the quoted line; "
            "write the lesson there first, or quote the line that says it"
        )
    return str(path.relative_to(workspace))


def _resolve(
    db: sqlite3.Connection, thread_id: str, destination: str, workspace: Path
) -> Path | None:
    from core.placement_answer import thread_place

    given = Path(destination).expanduser()
    roots = [workspace]
    tray = thread_place(db, thread_id).tray
    if tray is not None:
        roots.insert(0, tray.parent.parent)
    candidates = [given] if given.is_absolute() else [root / given for root in roots]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(workspace) and resolved.is_file():
            return resolved
    return None


def offered_skills(db: sqlite3.Connection, thread_id: str, workspace: Path):
    """The thread's skills as its next installed index would split them."""
    from core.lesson_index import partition
    from core.placement_answer import thread_place
    from core.skills_registry import resolve_effective_skills

    skills = resolve_effective_skills(
        thread_id=thread_id, project_id=thread_place(db, thread_id).project_id,
        workspace=workspace, db=db,
    )
    return partition(skills.included, skills.omitted, thread_id=thread_id, db=db)


def _held_trial(skills, path: Path) -> str | None:
    """Why ``path`` is a trial lesson the next index will not offer, or None."""
    from core.lesson_index import HELD_PREFIX

    for row in skills.omitted:
        reason = next((error for error in row.errors if error.startswith(HELD_PREFIX)), None)
        if reason is not None and row.path.resolve() == path:
            return reason
    return None


def _delivered(
    db: sqlite3.Connection, thread_id: str, path: Path, workspace: Path, skills
) -> bool:
    """Whether a later turn of this thread receives ``path``.

    Each answer comes from the owner of that delivery. A skill counts when it
    is one of the thread's effective skills (``resolve_effective_skills``), so
    a shadowed, duplicated or invalid ``SKILL.md`` does not, and an ancestor
    thread's delivered skill does; a trial lesson counts only while its owner
    holds its review (``lesson_index.partition``, as ``skills``). A bootloader
    or other context file counts
    when a context-source loader that reaches this thread loads it
    (``classify_context_source``). The agent reference has no loader: the
    roster entry's reference directory, as ``thread_place`` reads it, is what
    the agent is pointed at, so a Markdown file directly in it counts.
    """
    from core.context_source_paths import classify_context_source
    from core.placement_answer import thread_place

    place = thread_place(db, thread_id)
    reference = place.reference_dir
    if reference is not None and path.parent == reference.resolve():
        return path.suffix == ".md"
    if any(row.path.resolve() == path for row in skills.offered):
        return True
    reaches = {
        "workspace_context": "workspace",
        "project_context": place.project_id,
        "thread_context": thread_id,
    }
    return any(
        change.object_id == reaches.get(change.object_kind)
        for change in classify_context_source(path, workspace=workspace, db=db)
    )
