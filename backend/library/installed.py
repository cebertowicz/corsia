"""Read installed library copies without a second inventory or state store."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from core.skill_sidecar import installed_digest, installed_file_digests, parse_sidecar, read_sidecar


def local_bundle(folder: Path) -> tuple[dict[str, bytes] | None, list[str]]:
    """Return visible regular files, or changed-path evidence when the folder is unsafe.

    Hidden files are intentionally ignored so Finder metadata does not turn a
    reviewed copy into an edited one. A link or unreadable entry is reported as
    an edit and is never followed.
    """
    files: dict[str, bytes] = {}
    problems: list[str] = []
    try:
        for root, directories, names in os.walk(folder, followlinks=False):
            relative_root = Path(root).relative_to(folder)
            visible_directories = []
            for name in sorted(directories):
                if name.startswith("."):
                    continue
                relative = (relative_root / name).as_posix()
                try:
                    if stat.S_ISDIR((Path(root) / name).lstat().st_mode):
                        visible_directories.append(name)
                    else:
                        problems.append(relative)
                except OSError:
                    problems.append(relative)
            directories[:] = visible_directories
            for name in sorted(names):
                if name.startswith("."):
                    continue
                path = Path(root) / name
                relative = (relative_root / name).as_posix()
                try:
                    mode = path.lstat().st_mode
                    if not stat.S_ISREG(mode):
                        problems.append(relative)
                        continue
                    files[relative] = path.read_bytes()
                except OSError:
                    problems.append(relative)
    except OSError:
        return None, ["skill folder"]
    return files, problems


def local_edits(folder: Path, origin: dict[str, Any]) -> tuple[bool, list[str], bool]:
    """Compare a live installed folder with the writer's bundle digest.

    Returns whether the copy was edited, the paths to review, and whether those
    paths are exactly the changed ones. The whole-folder digest alone decides
    whether the copy was edited; the per-file map only narrows the list.
    """
    files, problems = local_bundle(folder)
    if files is None:
        return True, problems, True
    expected = origin.get("installed_digest")
    try:
        current = installed_file_digests(files)
        actual = installed_digest(files)
    except (UnicodeDecodeError, ValueError):
        return True, sorted(set([*problems, "corsia.yaml"])), True
    if isinstance(expected, str) and actual == expected and not problems:
        return False, [], True
    recorded = origin.get("installed_files")
    if isinstance(recorded, dict):
        changed = {path for path in {*recorded, *current} if recorded.get(path) != current.get(path)}
        if changed or problems:
            return True, sorted(changed | set(problems)), True
    # A copy installed before per-file digests (or whose map no longer matches
    # its digest) cannot say which file changed; list every file to review.
    return True, sorted(set([*files, *problems])), False


def read_origin(skill_folder: Path) -> dict[str, Any] | None:
    """Read a validated-style origin mapping from a local library copy."""
    text, problem = read_sidecar(skill_folder)
    if problem or text is None:
        return None
    sidecar, errors = parse_sidecar(text)
    if errors or not isinstance(sidecar, dict):
        return None
    origin = sidecar.get("origin")
    return dict(origin) if isinstance(origin, dict) else None
