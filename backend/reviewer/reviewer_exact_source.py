"""Read-only reviewer access to one immutable PR revision."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


class ExactSourceError(RuntimeError):
    """The reviewed base/head could not be grounded in the object store."""


def claude_exact_source_controls(
    git_dir: str, base_sha: str, head_sha: str,
) -> tuple[list[str], dict[str, str]]:
    """Return Claude's narrow exact-source tool and Git environment controls."""
    _require_sha(base_sha, "base_sha")
    _require_sha(head_sha, "head_sha")
    resolved = str(Path(git_dir).resolve())
    git = f"git --git-dir={shlex.quote(resolved)}"
    revision_range = f"{base_sha}...{head_sha}"
    args = [
        "--restricted",
        "--safe-mode",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--tools", "Bash",
        "--allowedTools",
        f"Bash({git} diff --no-ext-diff --no-textconv --stat {revision_range})",
        f"Bash({git} diff --no-ext-diff --no-textconv --name-only {revision_range})",
        f"Bash({git} diff --no-ext-diff --no-textconv --find-renames {revision_range} *)",
        f"Bash({git} show {head_sha}:*)",
        f"Bash({git} grep -n -F -e * {head_sha} -- *)",
        "--add-dir", resolved,
    ]
    env = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return args, env


def codex_exact_source_controls(review_dir: str, git_dir: str) -> list[str]:
    """Confine Codex shell reads to runtime files and this source store."""
    filesystem = {
        ":minimal": "read",
        str(Path(review_dir).resolve()): "read",
        str(Path(git_dir).resolve()): "read",
    }
    inline_table = "{" + ",".join(
        f"{json.dumps(path)}={json.dumps(access)}"
        for path, access in filesystem.items()
    ) + "}"
    return [
        "--strict-config",
        "-c", 'approval_policy="never"',
        "-c", "allow_login_shell=false",
        "-c", 'default_permissions="corsia_reviewer"',
        "-c", f"permissions.corsia_reviewer.filesystem={inline_table}",
        "-c", "permissions.corsia_reviewer.network.enabled=false",
        "-c", 'web_search="disabled"',
    ]


def _run_git(args: list[str], *, cwd: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _require_sha(sha: str, label: str) -> None:
    if not _SHA_RE.fullmatch(sha):
        raise ExactSourceError(f"{label} must be a 40-character lowercase SHA")


def _commit_exists(git_dir: str, sha: str, *, cwd: str) -> bool:
    return _run_git(
        [f"--git-dir={git_dir}", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=cwd,
    ).returncode == 0


def _ensure_commit(git_dir: str, sha: str, *, cwd: str) -> None:
    if _commit_exists(git_dir, sha, cwd=cwd):
        return
    fetched = _run_git(
        ["fetch", "--no-tags", "--no-write-fetch-head", "origin", sha],
        cwd=cwd,
        timeout=120,
    )
    if fetched.returncode or not _commit_exists(git_dir, sha, cwd=cwd):
        raise ExactSourceError(
            f"could not ground reviewed commit {sha[:12]}: git fetch failed"
        )


def _prepare_source(repo_dir: str, base_sha: str, head_sha: str) -> tuple[str, str]:
    _require_sha(base_sha, "base_sha")
    _require_sha(head_sha, "head_sha")
    resolved = _run_git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo_dir,
    )
    if resolved.returncode or not resolved.stdout.strip():
        raise ExactSourceError("reviewer source checkout has no readable Git object store")
    git_dir = str(Path(resolved.stdout.strip()).resolve())
    _ensure_commit(git_dir, base_sha, cwd=repo_dir)
    _ensure_commit(git_dir, head_sha, cwd=repo_dir)
    ancestry = _run_git(
        [f"--git-dir={git_dir}", "merge-base", base_sha, head_sha],
        cwd=repo_dir,
    )
    if ancestry.returncode or not ancestry.stdout.strip():
        raise ExactSourceError("exact reviewed base/head ancestry is unavailable")

    git = f"git --git-dir={shlex.quote(git_dir)}"
    instructions = f"""\
Read only the immutable reviewed change {base_sha}...{head_sha}. Start with:
- {git} diff --no-ext-diff --no-textconv --stat {base_sha}...{head_sha}
- {git} diff --no-ext-diff --no-textconv --name-only {base_sha}...{head_sha}
- {git} diff --no-ext-diff --no-textconv --find-renames {base_sha}...{head_sha}
Read exact files with `{git} show {head_sha}:<path>` and search the exact tree
with `{git} grep -n -F -e <pattern> {head_sha} -- <pathspec>`.
The working directory is intentionally not a checkout. Never use bare `cat`,
`rg`, or working-tree files as evidence; they cannot represent this revision.
"""
    return instructions, git_dir


def exact_review_diff(repo_dir: str, base_sha: str, head_sha: str) -> str:
    """Return the immutable PR diff used for context and local review."""
    _, git_dir = _prepare_source(repo_dir, base_sha, head_sha)
    result = _run_git(
        [
            f"--git-dir={git_dir}", "diff", "--no-ext-diff", "--no-textconv",
            "--find-renames", f"{base_sha}...{head_sha}",
        ],
        cwd=repo_dir,
        timeout=120,
    )
    if result.returncode:
        raise ExactSourceError("could not read exact reviewed diff: git diff failed")
    return result.stdout


@contextmanager
def exact_review_source(
    repo_dir: str, base_sha: str, head_sha: str,
) -> Iterator[tuple[str, str, str]]:
    """Yield exact-read instructions, neutral cwd, and the canonical Git dir."""
    source, git_dir = _prepare_source(repo_dir, base_sha, head_sha)
    with tempfile.TemporaryDirectory(prefix="corsia-review-") as review_dir:
        yield source, review_dir, git_dir
