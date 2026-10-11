"""Base-checkout Git paths a linked worktree must never mutate."""

from __future__ import annotations

from pathlib import Path


def base_git_identity_paths(repo: Path) -> list[Path]:
    """Return base-checkout Git files a linked worktree must never mutate."""
    git_dir = repo.resolve(strict=False) / ".git"
    protected = [
        git_dir / "HEAD",
        git_dir / "index",
        git_dir / "logs" / "HEAD",
    ]
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return protected
    prefix = "ref: refs/heads/"
    if not head.startswith(prefix):
        return protected
    branch = head.removeprefix(prefix)
    branch_path = Path(branch)
    if not branch or branch_path.is_absolute() or ".." in branch_path.parts:
        return protected
    ref = git_dir / "refs" / "heads" / branch
    log = git_dir / "logs" / "refs" / "heads" / branch
    return [*protected, ref, Path(f"{ref}.lock"), log, Path(f"{log}.lock")]


def base_branch_selector_paths(repo: Path) -> list[Path]:
    """Return metadata that selects the branch attached to the base checkout."""
    git_dir = repo.resolve(strict=False) / ".git"
    return [git_dir / "HEAD", git_dir / "HEAD.lock"]


def base_git_control_paths(repo: Path) -> list[Path]:
    """Return shared Git controls no agent command may rewrite."""
    git_dir = repo.resolve(strict=False) / ".git"
    return [
        git_dir / "config",
        git_dir / "config.lock",
        git_dir / "packed-refs",
        git_dir / "packed-refs.lock",
    ]
