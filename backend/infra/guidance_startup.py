"""Startup-only guidance history work."""
from __future__ import annotations

from pathlib import Path

from loguru import logger


def startup_sweep(workspace: Path) -> None:
    """Commit outside guidance edits once at server start; without a repository, do nothing."""
    from infra.guidance_git import _acquire, _locked, _sweep_others, workspace_repository

    repo, _why = workspace_repository(workspace)
    if repo is None:
        return
    try:
        with _locked(_acquire(repo)[0]) as db:
            lines = _sweep_others(repo, workspace, db, set())
        if lines:
            logger.info("startup guidance sweep: {}", "; ".join(lines))
    except Exception:  # startup must not fail on history
        logger.opt(exception=True).warning("startup guidance sweep failed")
