"""Update availability for the serving checkout's current branch.

Answers one question for the UI: does the branch this instance is serving have
commits on its remote that are not on disk yet?

The result is cached process-wide with a TTL because the underlying check runs
``git fetch`` — a network call. Poll frequency in the browser therefore does not
determine how often the remote is contacted, and several open tabs share one
cached answer. This mirrors the existing ``/api/system`` cache, just with a much
longer TTL because updates land far less often than CPU/memory changes.

An unreachable remote, a detached HEAD, or any git failure yields
``available: None`` (unknown) — never a confident "up to date", which would be a
false negative that hides a real update.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# Serving checkout root: backend/infra/update_status.py -> repo root.
_SERVING_ROOT = Path(__file__).resolve().parent.parent.parent

# The remote is contacted at most once per this window, regardless of poll rate.
CACHE_TTL_SECONDS = 300

_cache: dict = {"data": None, "ts": 0.0}


@dataclass(frozen=True)
class UpdateStatus:
    """What the UI needs to decide whether to offer an update."""

    branch: str | None
    current_sha: str | None
    remote_sha: str | None
    commits_behind: int | None
    available: bool | None
    checked_at: float | None
    error: str | None
    current_version: str | None
    target_version: str | None
    changes: dict | None

    def to_dict(self) -> dict:
        return asdict(self)


def _git(args: list[str], timeout: int = 30) -> str:
    """Run one git command in the serving checkout; raise on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=_SERVING_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:200])
    return result.stdout.strip()


def _unknown(error: str) -> UpdateStatus:
    """Unknown state — the UI must not claim the instance is up to date."""
    return UpdateStatus(
        branch=None,
        current_sha=None,
        remote_sha=None,
        commits_behind=None,
        available=None,
        checked_at=time.time(),
        error=error,
        current_version=None,
        target_version=None,
        changes=None,
    )


def _probe() -> UpdateStatus:
    """Fetch the current branch's remote ref and compare it with HEAD."""
    try:
        branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"])
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return _unknown("Serving checkout is in detached HEAD; no branch to check.")

    try:
        current = _git(["rev-parse", "HEAD"])
        _git(["fetch", "--quiet", "origin", branch])
        remote = _git(["rev-parse", f"origin/{branch}"])
    except subprocess.TimeoutExpired:
        return _unknown("Update check timed out contacting the remote.")
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return _unknown(f"Could not reach the remote: {exc}")

    # Only commits the remote has and we do not constitute an update. A branch
    # that is merely *ahead* (local commits not yet pushed) differs from its
    # remote SHA but has nothing to pull, so SHA inequality is not the signal.
    try:
        counts = _git(["rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"])
        behind = int(counts.split()[1])
    except (RuntimeError, OSError, ValueError, IndexError, subprocess.SubprocessError):
        # Without a reliable count we cannot tell "ahead" from "behind"; report
        # unknown rather than guessing an update in either direction.
        return UpdateStatus(
            branch=branch,
            current_sha=current,
            remote_sha=remote,
            commits_behind=None,
            available=None,
            checked_at=time.time(),
            error="Could not determine how far behind the remote this checkout is.",
            current_version=None,
            target_version=None,
            changes=None,
        )

    available = behind > 0
    from infra.update_changes import build_update_details

    details = build_update_details(available, branch, _git)
    return UpdateStatus(
        branch=branch,
        current_sha=current,
        remote_sha=remote,
        commits_behind=behind,
        available=available,
        checked_at=time.time(),
        error=None,
        current_version=details.current_version,
        target_version=details.target_version,
        changes=details.changes,
    )


def update_status(force: bool = False) -> UpdateStatus:
    """Return the cached update status, refreshing it when the TTL has expired."""
    now = time.monotonic()
    cached = _cache["data"]
    if not force and cached is not None and now - _cache["ts"] < CACHE_TTL_SECONDS:
        return cached

    status = _probe()
    _cache["data"] = status
    _cache["ts"] = now
    return status


def cached_target_version() -> str | None:
    """Return the cached target version without probing the remote."""
    cached = _cache["data"]
    return cached.target_version if cached is not None else None


def invalidate_cache() -> None:
    """Drop the cached answer so the next read re-probes the remote."""
    _cache["data"] = None
    _cache["ts"] = 0.0
