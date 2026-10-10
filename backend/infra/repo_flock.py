"""One advisory lock per registered repository for backend Git mutators.

The worktree reaper and the controlled base-checkout fast-forward both change
a repository's shared ``.git`` state. They serialize on the same ``fcntl``
lock, kept under the state root so no agent sandbox can hold or remove it.
The lock file is never deleted; ``flock`` releases with the descriptor.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import time

from infra.project_repo_policy import (
    RepoFastForward,
    RepoGitRunner,
    RepoPolicy,
    RepoPolicyLoader,
    fast_forward_repo,
)

_POLL_SECONDS = 0.1
_FAST_FORWARD_MAX_WAIT_SECONDS = 30.0


class RepoBusyError(RuntimeError):
    """Another backend Git mutator holds this repository's lock."""


def _lock_dir() -> Path:
    from infra.config import STATE_ROOT

    return STATE_ROOT / "locks"


def repo_lock_path(repo: Path) -> Path:
    """Return the lock file for one canonical repository path."""
    canonical = str(repo.expanduser().resolve(strict=False))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return _lock_dir() / f"worktree-{digest}.lock"


@contextmanager
def repo_lock(repo: Path, *, wait_seconds: float = 0.0) -> Iterator[None]:
    """Hold the repository lock, waiting at most ``wait_seconds`` for it.

    Raises ``RepoBusyError`` when the lock stays held, and ``OSError`` when
    the lock file cannot be opened; callers treat both as "do not proceed".
    """
    path = repo_lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RepoBusyError(
                        f"repository {repo} is busy with another Git maintenance step"
                    ) from None
                time.sleep(_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def fast_forward_repo_locked(
    policy: RepoPolicy,
    *,
    runner: RepoGitRunner,
    policy_loader: RepoPolicyLoader,
    timeout: float,
) -> RepoFastForward:
    """Run ``fast_forward_repo`` under the repository lock.

    The reaper holds the lock only for one worktree removal at a time, so the
    wait is short; the time spent waiting comes out of the fast-forward budget.
    """
    started = time.monotonic()
    with repo_lock(
        policy.path, wait_seconds=min(timeout, _FAST_FORWARD_MAX_WAIT_SECONDS),
    ):
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("controlled fast-forward timed out waiting for its lock")
        return fast_forward_repo(
            policy, runner=runner, policy_loader=policy_loader, timeout=remaining,
        )
