"""Build cached version and change details for the update-status projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import subprocess


Git = Callable[[list[str]], str]
_GIT_ERRORS = (RuntimeError, OSError, subprocess.SubprocessError)
_CHANGELOG_HEADING = re.compile(r"^## v(?P<version>[^\s—]+)\s+—\s*(?P<headline>.*)$")


@dataclass(frozen=True)
class UpdateDetails:
    """Version and release-note detail derived during one update probe."""

    current_version: str | None
    target_version: str | None
    changes: dict | None


def _tag_version(git: Git, ref: str) -> str | None:
    try:
        tag = git(["describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", ref])
    except _GIT_ERRORS:
        return None
    return tag.removeprefix("v") or None


def _commit_date(git: Git, ref: str) -> datetime | None:
    try:
        timestamp = int(git(["show", "-s", "--format=%ct", ref]))
    except _GIT_ERRORS + (ValueError,):
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc)


def _published_details(git: Git, branch: str) -> UpdateDetails:
    current_version = _tag_version(git, "HEAD")
    try:
        changelog = git(["show", f"origin/{branch}:CHANGELOG.md"])
    except _GIT_ERRORS:
        changelog = None
    target_version, headline = _changelog_target(changelog)
    if target_version is None:
        target_version = _tag_version(git, f"origin/{branch}")
    changes = None
    if target_version is not None and changelog is not None:
        changes = _changelog_changes(changelog, target_version, headline)
    return UpdateDetails(current_version, target_version, changes)


def _changelog_target(content: str | None) -> tuple[str | None, str | None]:
    if content is None:
        return None, None
    for line in content.splitlines():
        match = _CHANGELOG_HEADING.match(line)
        if match:
            return match.group("version"), match.group("headline") or None
    return None, None


def _changelog_changes(content: str, target_version: str, headline: str | None) -> dict | None:
    lines = content.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = _CHANGELOG_HEADING.match(line)
        if match and match.group("version") == target_version:
            start = index + 1
            if headline is None:
                headline = match.group("headline") or None
            break
    if start is None:
        return None
    bullets: list[str] = []
    in_subsection = False
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.startswith("### "):
            in_subsection = True
        elif in_subsection and line.startswith("- "):
            bullets.append(line[2:])
    return {
        "kind": "changelog",
        "headline": headline,
        "items": bullets[:50],
        "more": max(0, len(bullets) - 50),
    }


def _development_details(git: Git, branch: str) -> UpdateDetails:
    current_date = _commit_date(git, "HEAD")
    target_date = _commit_date(git, f"origin/{branch}")
    current_version = current_date.strftime("%Y.%m.%d") if current_date else None
    target_version = target_date.strftime("%Y.%m.%d") if target_date else None
    if current_date and target_date and current_version == target_version:
        current_version = current_date.strftime("%Y.%m.%d.%H%M")
        target_version = target_date.strftime("%Y.%m.%d.%H%M")
    try:
        subjects = git(["log", "--format=%s", f"HEAD..origin/{branch}"]).splitlines()
    except Exception:
        return UpdateDetails(current_version, target_version, None)
    subjects = [subject for subject in subjects if not subject.startswith("Merge ")]
    return UpdateDetails(
        current_version,
        target_version,
        {"kind": "commits", "headline": None, "items": subjects[:50], "more": max(0, len(subjects) - 50)},
    )


def _current_version_only(git: Git, branch: str) -> str | None:
    if branch == "main":
        return _tag_version(git, "HEAD")
    current_date = _commit_date(git, "HEAD")
    return current_date.strftime("%Y.%m.%d") if current_date else None


def build_update_details(available: bool | None, branch: str | None, git: Git) -> UpdateDetails:
    """Return the version on disk, and target and changes only when the probe found an update."""
    if branch is None:
        return UpdateDetails(None, None, None)
    if available is not True:
        return UpdateDetails(_current_version_only(git, branch) if available is False else None, None, None)
    try:
        return _published_details(git, branch) if branch == "main" else _development_details(git, branch)
    except Exception:
        return UpdateDetails(None, None, None)
