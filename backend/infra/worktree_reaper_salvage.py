"""Copy uncommitted work out of an idle worktree before removal."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess

from infra.worktree_reaper_probe import GIT_TIMEOUT_SECONDS, _is_disposable


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
    for record in records:
        if record.startswith("2 "):
            next(records, None)  # original path of a rename
        if not record.startswith(("? ", "! ")):
            continue
        name = record[2:]
        if _is_disposable(name):
            continue
        path = worktree / name
        if name.endswith("/") and not path.is_symlink():
            def fail(exc: OSError) -> None:
                raise exc

            for current, dirs, files in os.walk(path, followlinks=False, onerror=fail):
                root = Path(current)
                for directory in dirs[:]:
                    child = root / directory
                    relative = child.relative_to(worktree).as_posix()
                    if _is_disposable(relative + "/"):
                        dirs.remove(directory)
                    elif child.is_symlink():
                        dirs.remove(directory)
                        selected.append(child.relative_to(worktree))
                selected.extend(
                    (root / filename).relative_to(worktree) for filename in files
                    if not _is_disposable((root / filename).relative_to(worktree).as_posix())
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
        patch = _git_output(worktree, ["diff", "HEAD", "--binary"])
        if patch:
            (destination / "changes.patch").write_bytes(patch)
        selected = _selected(worktree)
        for relative in selected:
            target = destination / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(worktree / relative, target, follow_symlinks=False)
        if any(not os.path.lexists(destination / "files" / relative) for relative in selected):
            raise SalvageError("copied file is missing")
        return destination
    except (OSError, UnicodeError, SalvageError) as exc:
        if created:
            shutil.rmtree(destination)
        raise SalvageError(str(exc)) from exc
