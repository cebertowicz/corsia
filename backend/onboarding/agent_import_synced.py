"""Shared local discovery of personal Claude-synced skills."""

from __future__ import annotations

import datetime
import json
import os
import stat
from pathlib import Path


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def real_folder(folder: Path, root: Path) -> bool:
    """The folder is not a link and resolves inside the checked root."""
    try:
        return stat.S_ISDIR(os.lstat(folder).st_mode) and inside(Path(os.path.realpath(folder)), root)
    except OSError:
        return False


def dirs(folder: Path, root: Path) -> list[Path]:
    """Visible entries of a real folder; links are kept for later refusal."""
    try:
        if not real_folder(folder, root):
            return []
        return sorted(Path(entry.path) for entry in os.scandir(folder)
                      if not entry.name.startswith(".") and not entry.is_file(follow_symlinks=False))
    except OSError:
        return []


def synced_skills(root: Path) -> list[Path]:
    """Personal synced skills, choosing the newest copy across account buckets."""
    latest: dict[str, tuple[datetime.datetime, Path]] = {}
    synced = root / "skills" / "synced"
    for bucket in dirs(synced, root):
        if not real_folder(bucket, root):
            continue
        try:
            manifest = bucket / "manifest.json"
            if not stat.S_ISREG(os.lstat(manifest).st_mode):
                continue
            entries = json.loads(manifest.read_text(encoding="utf-8"))["skills"]
            if not isinstance(entries, list):
                continue
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name, source = entry.get("name"), entry.get("source") or "custom"
            if (not isinstance(name, str) or not name or name.startswith(".") or Path(name).name != name
                    or not isinstance(source, str) or source in ("anthropic", "anthropic-example")):
                continue
            path = bucket / name
            if not os.path.lexists(path):
                continue
            try:
                updated = datetime.datetime.fromisoformat(entry.get("updatedAt", "").replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=datetime.timezone.utc)
                updated = updated.astimezone(datetime.timezone.utc)
            except (ValueError, TypeError, AttributeError, OverflowError):
                updated = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
            if name not in latest or updated > latest[name][0]:
                latest[name] = (updated, path)
    return [latest[name][1] for name in sorted(latest)]


def synced_personal_count(root: Path) -> int:
    """Return only the count from local synced manifests for pre-consent detection."""
    return len(synced_skills(root))
