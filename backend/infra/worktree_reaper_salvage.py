"""Copy uncommitted work out of an idle worktree before removal."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess

from infra.worktree_reaper_probe import (
    GIT_TIMEOUT_SECONDS,
    _is_disposable,
    is_tool_output_dir,
)


class SalvageError(RuntimeError):
    """A worktree could not be completely salvaged."""


def _git_output(worktree: Path, args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", *args],
            cwd=worktree, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SalvageError(f"git {args[0]} failed: {exc}") from exc
    if result.returncode:
        raise SalvageError(f"git {args[0]} failed: {os.fsdecode(result.stderr)[:240]}")
    return result.stdout


def _selected(worktree: Path) -> list[Path]:
    status = _git_output(worktree, [
        "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching",
    ])
    records = iter(os.fsdecode(status).split("\0"))
    selected: list[Path] = []
    marked: dict[Path, bool] = {}

    def in_tool_output(relative: Path) -> bool:
        # Untracked files are listed one by one, so a virtualenv or cache
        # shows up only through the marker in one of its parents.
        for parent in list(relative.parents)[:-1]:
            if parent not in marked:
                marked[parent] = is_tool_output_dir(worktree / parent)
            if marked[parent]:
                return True
        return False
    for record in records:
        if record.startswith("2 "):
            next(records, None)  # original path of a rename
        if not record.startswith(("? ", "! ")):
            continue
        # Untracked paths are not gitignored, so they are someone's work: only a
        # self-marked virtualenv or cache is left out. Ignored paths also drop
        # the named cache directories the repository ignores.
        ignored = record.startswith("! ")
        name = record[2:]
        if (ignored and _is_disposable(name)) or in_tool_output(Path(name)):
            continue
        path = worktree / name
        if name.endswith("/") and not path.is_symlink():
            if is_tool_output_dir(path):
                continue

            def fail(exc: OSError) -> None:
                raise exc

            for current, dirs, files in os.walk(path, followlinks=False, onerror=fail):
                root = Path(current)
                for directory in dirs[:]:
                    child = root / directory
                    relative = child.relative_to(worktree).as_posix()
                    if child.is_symlink():
                        dirs.remove(directory)
                        selected.append(child.relative_to(worktree))
                    elif is_tool_output_dir(child) or (
                        ignored and _is_disposable(relative + "/")
                    ):
                        dirs.remove(directory)
                selected.extend(
                    (root / filename).relative_to(worktree) for filename in files
                    if not (ignored and _is_disposable(
                        (root / filename).relative_to(worktree).as_posix()
                    ))
                )
        else:
            selected.append(Path(name))
    return selected


def salvage_worktree(repo: Path, worktree: Path, name: str, now: float) -> Path:
    """Copy tracked changes and non-disposable scratch, or remove the partial copy."""
    root = repo.parent / f"{repo.name}-salvage" / datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    destination = root / name
    suffix = 1
    while os.path.lexists(destination):
        destination = root / f"{name}-{suffix}"
        suffix += 1
    created = False
    try:
        destination.mkdir(parents=True)
        created = True
        branch = os.fsdecode(_git_output(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])).strip()
        head = os.fsdecode(_git_output(worktree, ["rev-parse", "HEAD"])).strip()
        (destination / "meta.txt").write_text(
            f"worktree: {worktree}\nbranch: {branch}\nHEAD: {head}\n", encoding="utf-8",
            errors="surrogateescape",
        )
        # The person's diff settings (external tool, textconv, color) would
        # write display output instead of a patch that applies.
        patch = _git_output(worktree, [
            "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv", "--no-color",
        ])
        if patch:
            (destination / "changes.patch").write_bytes(patch)
        selected = _selected(worktree)
        for relative in selected:
            target = destination / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(worktree / relative, target, follow_symlinks=False)
        return destination
    except (OSError, UnicodeError, SalvageError) as exc:
        if created:
            shutil.rmtree(destination)
        raise SalvageError(str(exc)) from exc
