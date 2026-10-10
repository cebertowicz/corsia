"""Guidance-history helpers backed by the canonical Git runner."""
from __future__ import annotations

import hashlib
from pathlib import Path


def digest(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def find_commit(repo: Path, trailer: str, path: Path) -> str | None:
    from infra.guidance_git import _git

    found = _git(repo, "log", "-1", "--format=%H", "--fixed-strings", f"--grep={trailer}", "--", str(path), ok=(0, 128))
    return found.strip() or None


def file_version(repo: Path, path: Path) -> str | None:
    from infra.guidance_git import _changed, _git

    rel = path.resolve(strict=False).relative_to(repo.resolve(strict=False)).as_posix()
    commit = _git(repo, "log", "-1", "--format=%H", "--", f":(literal){rel}").strip()
    if not commit:
        return None
    return commit + ("·dirty" if _changed(repo, [f":(literal){rel}"]) else "")
