"""Read-only Git and process probes for the worktree reaper.

Every Git call here runs with ``--no-optional-locks`` so that probing a
worktree never refreshes its index and resets the idle clock, with hooks
disabled as ``tools/worktrees.py`` does, and in the C locale so the stderr
text the removal path matches is Git's untranslated English. Every probe
fails closed: an error becomes a skip, never a "safe to remove".
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

GIT_TIMEOUT_SECONDS = 30.0
LSOF_TIMEOUT_SECONDS = 60.0
GH_TIMEOUT_SECONDS = 60.0

# Ignored directories rebuilt from lockfiles or caches, gate scratch directories,
# generated build metadata, and symlinked dependency entries. Any other ignored
# path (``.env``, seeded databases, local notes) blocks removal.
DISPOSABLE_DIRS = frozenset({
    "node_modules", ".venv", "__pycache__", ".pytest_cache", "dist", "build",
    ".svelte-kit", ".mypy_cache", ".ruff_cache", "test-results", "playwright-report",
})
DISPOSABLE_TOP_LEVEL_DIR_PREFIXES = (".check-pr-",)
DISPOSABLE_FILES = frozenset({
    "frontend/static/build-info.json", "frontend/src/lib/build-info.json",
})
DISPOSABLE_NON_DIRECTORY_NAMES = frozenset({"node_modules", ".venv"})


class ReaperGitError(RuntimeError):
    """A Git probe could not produce a trustworthy answer."""


def git(
    cwd: Path, args: list[str], *, timeout: float = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run one Git command without optional locks, hooks or translated output."""
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise ReaperGitError(f"git {' '.join(args[:2])} failed: {exc}") from exc


def git_detail(result: subprocess.CompletedProcess[str]) -> str:
    """Return a bounded one-line failure detail for logs and metrics."""
    return ((result.stderr or result.stdout).strip() or "git failed")[:240]


def is_ancestor(repo: Path, head: str, target: str) -> bool:
    """Return whether ``head`` is ``target`` or one of its ancestors."""
    if head == target:
        return True
    return git(repo, ["merge-base", "--is-ancestor", head, target]).returncode == 0


def has_unpublished_commits(repo: Path, head: str) -> bool:
    """Return whether ``head`` holds a commit no remote-tracking ref contains.

    Fails closed: a Git error or timeout counts as unpublished.
    """
    try:
        result = git(repo, ["rev-list", "--count", head, "--not", "--remotes"])
    except ReaperGitError:
        return True
    return result.returncode != 0 or result.stdout.strip() != "0"


_GITHUB_REMOTE = re.compile(
    r"github\.com[:/]+(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$", re.IGNORECASE,
)


def github_remote_full_name(repo: Path, remote: str) -> str | None:
    """Return ``owner/repo`` for a named GitHub remote, else None."""
    result = git(repo, ["config", "--get", f"remote.{remote}.url"])
    match = _GITHUB_REMOTE.search(result.stdout.strip()) if result.returncode == 0 else None
    return f"{match['owner']}/{match['name']}".lower() if match else None


def origin_full_name(repo: Path) -> str | None:
    """Return ``owner/repo`` for the GitHub ``origin`` remote, else None."""
    return github_remote_full_name(repo, "origin")


def open_pr_branches(repo: Path) -> set[str] | None:
    """Return the head branches of the repository's open PRs, or None if unknown.

    One bounded ``gh`` call per pass; any failure returns None so the caller
    keeps the longer threshold.
    """
    try:
        full_name = origin_full_name(repo)
        binary = shutil.which("gh")
        if full_name is None or binary is None:
            return None
        result = subprocess.run(
            [binary, "pr", "list", "--repo", full_name, "--state", "open",
             "--limit", "1000", "--json", "headRefName"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None
        return {str(item["headRefName"]) for item in json.loads(result.stdout)}
    except (ReaperGitError, OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None


@dataclass(frozen=True)
class WorktreeEntry:
    """One ``git worktree list --porcelain`` record."""

    path: Path
    head: str | None
    branch: str | None
    locked: bool
    prunable: bool


def list_worktrees(repo: Path) -> list[WorktreeEntry]:
    """Return every worktree Git has registered for ``repo``."""
    result = git(repo, ["worktree", "list", "--porcelain"])
    if result.returncode != 0:
        raise ReaperGitError(f"worktree list failed: {git_detail(result)}")
    entries: list[WorktreeEntry] = []
    for block in result.stdout.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            fields[key] = value
        if "worktree" not in fields:
            continue
        entries.append(WorktreeEntry(
            path=Path(fields["worktree"]).resolve(strict=False),
            head=fields.get("HEAD"),
            branch=fields.get("branch"),
            locked="locked" in fields,
            prunable="prunable" in fields,
        ))
    return entries


def admin_dir(worktree: Path) -> Path | None:
    """Return the ``.git/worktrees/<n>`` directory a worktree points at."""
    marker = worktree / ".git"
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        prefix, raw = marker.read_text(encoding="utf-8").strip().split(":", 1)
    except (OSError, ValueError):
        return None
    if prefix != "gitdir":
        return None
    path = Path(raw.strip())
    if not path.is_absolute():
        path = worktree / path
    return path.resolve(strict=False)


def git_lock_files(admin: Path) -> list[str]:
    """Return every ``*.lock`` file under a worktree's admin directory."""
    try:
        return sorted(
            str(path.relative_to(admin)) for path in admin.rglob("*.lock")
        )
    except OSError as exc:
        return [f"<unreadable: {exc.__class__.__name__}>"]


def _is_disposable(ignored: str) -> bool:
    parts = [part for part in ignored.rstrip("/").split("/") if part]
    directories = parts if ignored.endswith("/") else parts[:-1]
    return (
        ignored in DISPOSABLE_FILES
        or (
            bool(directories)
            and directories[0].startswith(DISPOSABLE_TOP_LEVEL_DIR_PREFIXES)
        )
        or (
            not ignored.endswith("/")
            and bool(parts)
            and parts[-1] in DISPOSABLE_NON_DIRECTORY_NAMES
        )
        or any(part in DISPOSABLE_DIRS for part in directories)
    )


def status_skip_reason(worktree: Path, *, tracked_only: bool = False) -> str | None:
    """Return ``dirty``, ``untracked`` or ``ignored_data``, or None when clean.

    ``tracked_only`` reports only uncommitted edits to tracked files: a merged
    worktree's untracked and ignored files are scratch, not unshipped work.
    """
    files = ["--untracked-files=no"] if tracked_only else [
        "--untracked-files=all", "--ignored=matching",
    ]
    result = git(worktree, ["status", "--porcelain=v2", *files])
    if result.returncode != 0:
        raise ReaperGitError(f"status failed: {git_detail(result)}")
    reasons: set[str] = set()
    for line in result.stdout.splitlines():
        kind, _, rest = line.partition(" ")
        if kind in {"1", "2", "u"}:
            reasons.add("dirty")
        elif kind == "?":
            reasons.add("untracked")
        elif kind == "!" and not _is_disposable(rest):
            reasons.add("ignored_data")
    for reason in ("dirty", "untracked", "ignored_data"):
        if reason in reasons:
            return reason
    return None


def _lsof(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    binary = shutil.which("lsof") or (
        "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None
    )
    if binary is None:
        return None
    try:
        return subprocess.run(
            [binary, *args], capture_output=True, text=True,
            timeout=LSOF_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _within(path: str, root: Path) -> bool:
    root_text = str(root)
    return path == root_text or path.startswith(root_text + os.sep)


class ProcessProbe:
    """Live-process checks: one cwd snapshot per run, ``+D`` per candidate."""

    def __init__(self) -> None:
        self._cwds: list[str] | None = None
        self._snapshotted = False

    def _snapshot(self) -> list[str] | None:
        result = _lsof(["-Fpn", "-d", "cwd"])
        if result is None or result.returncode != 0:
            return None
        return [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]

    def refresh(self) -> None:
        """Take a fresh cwd snapshot, as the pre-removal recheck requires."""
        self._cwds = self._snapshot()
        self._snapshotted = True

    def skip_reason(self, worktree: Path) -> str | None:
        """Return ``in_use`` or ``unknown_process``, or None when idle."""
        if not self._snapshotted:
            self.refresh()
        if self._cwds is None:
            return "unknown_process"
        if any(_within(cwd, worktree) for cwd in self._cwds):
            return "in_use"
        result = _lsof(["-t", "+D", str(worktree)])
        if result is None:
            return "unknown_process"
        if result.returncode == 0 and result.stdout.strip():
            return "in_use"
        # lsof exits 1 both for "nothing open" and for errors; only a silent
        # exit 1 means nothing is open.
        if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
            return None
        return "unknown_process"
