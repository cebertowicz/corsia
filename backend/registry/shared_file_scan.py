"""Bounded walks of a shared root: every file in it, and the ones that look like a new file's name."""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path


def bounded_shared_files(shared_root: Path, *, limit: int = 2000):
    pending = [shared_root]
    visited = 0
    while pending and visited < limit:
        current = pending.pop()
        try:
            children = sorted(
                current.iterdir(),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            continue
        directories: list[Path] = []
        for candidate in children:
            visited += 1
            if visited > limit:
                break
            try:
                if candidate.is_symlink():
                    continue
                if candidate.is_dir():
                    directories.append(candidate)
                elif candidate.is_file():
                    yield candidate
            except OSError:
                continue
        pending.extend(reversed(directories))


def similar_files(target: Path, shared_root: Path) -> list[str]:
    scored: list[tuple[float, str]] = []
    target_stem = target.stem.casefold()
    for candidate in bounded_shared_files(shared_root):
        if (
            candidate == target
            or candidate.name == "INDEX.md"
            or candidate.name.startswith(".")
        ):
            continue
        score = SequenceMatcher(None, target_stem, candidate.stem.casefold()).ratio()
        if score >= 0.45:
            scored.append((score, str(candidate)))
    scored.sort(key=lambda item: (-item[0], item[1].casefold()))
    return [path for _score, path in scored[:5]]
