"""Keep the workspace's real Git index in step with a guidance commit (issue #2322).

:func:`infra.guidance_git._commit` commits from a temporary ``GIT_INDEX_FILE`` and moves HEAD with
``update-ref``, so the person's own ``.git/index`` is a second, separate write that can fail on its own.
A stale ``index.lock`` makes every one of those syncs fail, the real index keeps the pre-save bytes, and
the person's next plain ``git commit`` writes those bytes back over the save. This module clears a
proven-stale lock through the one shared fail-closed rule, so the save's own sync can run.

It never removes a lock outside that rule and never writes the index: :mod:`infra.guidance_git` stays
the only writer of guidance history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from loguru import logger

from infra.guidance_states import GitFault
from infra.project_repo_lock_recovery import GitIndexLockRecoveryError, recover_stale_git_index_lock

Git = Callable[..., str]


def index_lock_fault(git: Git, repo: Path) -> str:
    """Clear a stale ``index.lock`` through the shared fail-closed rule before the real-index sync.

    Returns "" when there was nothing to clear or the lock is gone, and the rule's own fault text when
    it preserved a lock it could not prove stale. It never raises: this runs after ``update-ref``, so
    the commit has landed and a preserved lock must stay a fault in the result, never a failed save.
    """
    try:
        git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
        if not (git_dir / "index.lock").exists():
            return ""
        recover_stale_git_index_lock(git_dir)
    except (GitFault, GitIndexLockRecoveryError, OSError) as exc:
        logger.warning("guidance index lock in {} was preserved: {}", repo, exc)
        return str(exc)
    return ""
