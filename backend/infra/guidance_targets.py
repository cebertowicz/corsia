"""Guidance path classification and its stable target value."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

DOCUMENT_SCOPE = "document"


@dataclass(frozen=True)
class GuidanceTarget:
    """A history save's scope label, or the fault that hid it.

    ``document`` marks an ordinary document: recorded by the same writer, but none of the
    instruction-only work (the lesson check, the carry-in record) follows its save."""

    scope: str
    fault: str | None = None
    document: bool = False


def guidance_target(path: Path, workspace: Path | None = None) -> GuidanceTarget | None:
    """Return a guidance target, preserving classification faults for the caller."""
    from core.guidance_paths import classify_guidance_path
    from infra.config import WORKSPACE
    from infra.database import get_db

    try:
        with closing(get_db()) as db:
            scope = classify_guidance_path(path, workspace=workspace or Path(WORKSPACE), db=db)
    except Exception as exc:
        logger.warning("guidance classification failed for {}: {}", path, exc)
        return GuidanceTarget("unknown", f"could not check whether this file is guidance ({exc})")
    return GuidanceTarget(scope) if scope else None


def history_target(path: Path, workspace: Path | None = None) -> GuidanceTarget | None:
    """The target whose saves the workspace history records: guidance first, else a recorded document.

    A document whose classification fails is not recorded: its save stays an ordinary write, as it was
    before documents had history. One the workspace's Git ignores is a target, and the writer's own
    ignore check saves it unrecorded, so a save asks Git that once."""
    target = guidance_target(path, workspace)
    if target is not None:
        return target
    from infra.config import WORKSPACE

    if not document_candidate(path, Path(workspace or WORKSPACE)):
        return None
    return GuidanceTarget(DOCUMENT_SCOPE, document=True)


def document_candidate(path: Path, workspace: Path) -> bool:
    """The database half of the document decision (no Git); a failed check records nothing."""
    from core.document_history_paths import is_history_document
    from infra.database import get_db

    try:
        with closing(get_db()) as db:
            return is_history_document(path, workspace=workspace, db=db)
    except Exception as exc:
        logger.warning("document history classification failed for {}: {}", path, exc)
        return False


def ignored_by_workspace(path: Path, workspace: Path) -> bool:
    """Whether the workspace's own repository ignores ``path``; a Git fault counts as ignored."""
    from infra.guidance_git import _git, workspace_repository
    from infra.guidance_states import GitFault

    try:
        repo, _why = workspace_repository(workspace)
        if repo is None:
            return False
        relative = path.resolve(strict=False).relative_to(repo).as_posix()
        return bool(_git(repo, "check-ignore", "--", relative, ok=(0, 1)).strip())
    except (GitFault, ValueError) as exc:
        logger.warning("document ignore check failed for {}: {}", path, exc)
        return True
