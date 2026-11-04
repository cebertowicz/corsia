"""Read an earlier CORSIA workspace folder and say what can come over (issue #2125).

Only ``projects/<folder>/{context,skills,shared}`` and the workspace's own
``skills/`` and ``shared/`` are read. Chat history, settings, connected
accounts and the old database are never opened, and no path under ``.threads/``,
``source/`` or ``.corsia/`` is walked at all.

Every file is screened here, before any writer sees it: a symlink is refused, so
nothing outside the named folder is followed, and text is checked with the same
credential check the agent import uses for skills. A file that fails is left
behind and named in the result, never with its contents.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 500

#: Trees that never come over, whatever they hold.
SCREENED_DIRS = frozenset({".threads", "source", ".corsia"})
_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal")
#: Finder leaves this in any folder it opens; it is nobody's content, so it is never named.
_FINDER_METADATA = ".DS_Store"

LINK, NOT_TEXT, TOO_LARGE, TOO_MANY = "link", "not_text", "too_large", "too_many"
HAS_SECRET, SCREENED, DATABASE = "secret", "screened", "database"
NO_SKILL_MD, NO_DESCRIPTION, BAD_NAME = "no_skill_md", "no_description", "bad_name"

WORKSPACE = "workspace"


@dataclass(frozen=True)
class File:
    """One readable file, addressed by its path inside its scope."""

    relative: str
    data: bytes


@dataclass(frozen=True)
class Skill:
    """One readable skill bundle, named and described by its own SKILL.md."""

    name: str
    description: str
    files: dict[str, bytes]


@dataclass(frozen=True)
class LeftBehind:
    """One entry that stays where it is, and the one word for why."""

    where: str
    path: str
    reason: str


@dataclass
class Scope:
    """What one scope — the workspace, or one of its projects — offers."""

    label: str
    name: str = ""
    context: list[File] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    shared: list[File] = field(default_factory=list)


@dataclass
class Plan:
    """Everything an earlier install offers, with what it leaves behind."""

    source: Path
    workspace: Scope
    projects: list[Scope] = field(default_factory=list)
    left_behind: list[LeftBehind] = field(default_factory=list)


class Unreadable(ValueError):
    """The named folder is not an earlier CORSIA workspace this can read."""


def _screened(name: str) -> str | None:
    """The reason one directory entry never comes over, or None."""
    if name in SCREENED_DIRS or name.startswith("."):
        return SCREENED
    if any(name.casefold().endswith(suffix) for suffix in _DATABASE_SUFFIXES):
        return DATABASE
    return None


def _read(path: Path) -> bytes | str:
    """The file's bytes, or the reason it is left behind."""
    from host_access.credential_sanitizer import contains_credential

    entry = os.lstat(path)
    if not stat.S_ISREG(entry.st_mode):
        return LINK if stat.S_ISLNK(entry.st_mode) else NOT_TEXT
    if entry.st_size > MAX_FILE_BYTES:
        return TOO_LARGE
    with open(path, "rb") as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        return TOO_LARGE
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return NOT_TEXT
    if "\x00" in text:
        return NOT_TEXT
    return HAS_SECRET if contains_credential(text) else data


def _walk(root: Path, prefix: str, *, where: str, left: list[LeftBehind]) -> list[File]:
    """Every readable file under ``root``, screened entry by entry."""
    files: list[File] = []
    if not root.is_dir() or root.is_symlink():
        return files
    for directory, subfolders, names in os.walk(root):
        here = Path(directory)
        kept = []
        for name in sorted(subfolders):
            relative = (here / name).relative_to(root).as_posix()
            reason = _screened(name) or (LINK if (here / name).is_symlink() else None)
            if reason is None:
                kept.append(name)
            else:
                left.append(LeftBehind(where, f"{prefix}/{relative}", reason))
        subfolders[:] = kept
        for name in sorted(set(names) - {_FINDER_METADATA}):
            path = here / name
            relative = f"{prefix}/{path.relative_to(root).as_posix()}"
            screened = _screened(name)
            read = screened or (TOO_MANY if len(files) >= MAX_FILES else _read(path))
            if isinstance(read, bytes):
                files.append(File(relative, read))
            else:
                left.append(LeftBehind(where, relative, read))
    return files


def _entries(root: Path) -> list[Path]:
    """One folder's entries by name, without Finder's metadata."""
    return sorted(
        (entry for entry in root.iterdir() if entry.name != _FINDER_METADATA),
        key=lambda path: path.name,
    )


def read_bundle(folder: Path) -> Skill | str:
    """One skill bundle read through the same screening, or the reason it stays."""
    import yaml

    from core.skill_validation import parse_frontmatter, validate_skill_dir_name
    from core.skill_workflow_validation import text_value
    from onboarding.agent_import_skills import shown_description

    left: list[LeftBehind] = []
    files = {
        file.relative.lstrip("/"): file.data
        for file in _walk(folder, "", where="", left=left)
    }
    if left:
        return left[0].reason
    if "SKILL.md" not in files:
        return NO_SKILL_MD
    try:
        metadata, _body = parse_frontmatter(files["SKILL.md"].decode("utf-8"))
    except (ValueError, yaml.YAMLError):
        return NO_SKILL_MD
    try:
        name = validate_skill_dir_name(text_value(metadata.get("name")) or folder.name)
    except ValueError:
        return BAD_NAME
    description = shown_description(text_value(metadata.get("description")) or "")
    if not description:
        return NO_DESCRIPTION
    return Skill(name, description, files)


def _skills(root: Path, *, where: str, left: list[LeftBehind]) -> list[Skill]:
    """Each skill folder's bundle; one that cannot be read is named, not carried."""
    found: list[Skill] = []
    if not root.is_dir() or root.is_symlink():
        return found
    for entry in _entries(root):
        named = f"skills/{entry.name}"
        if (reason := _screened(entry.name)) is not None:
            left.append(LeftBehind(where, named, reason))
        elif entry.is_symlink():
            left.append(LeftBehind(where, named, LINK))
        elif not entry.is_dir():
            left.append(LeftBehind(where, named, NOT_TEXT))
        elif isinstance(bundle := read_bundle(entry), str):
            left.append(LeftBehind(where, named, bundle))
        else:
            found.append(bundle)
    return found


def project_name(context: list[File], folder: str) -> str:
    """The first heading of ``context/PROJECT.md``, else the folder name."""
    for file in context:
        if file.relative != "context/PROJECT.md":
            continue
        for line in file.data.decode("utf-8", "replace").splitlines():
            if line.startswith("# ") and line[2:].strip():
                return " ".join(line[2:].split())
    return folder


def _scope(root: Path, label: str, *, context: bool, left: list[LeftBehind]) -> Scope:
    """One scope's offered context, skills and shared documents."""
    scope = Scope(label)
    if context:
        scope.context = _walk(root / "context", "context", where=label, left=left)
    scope.skills = _skills(root / "skills", where=label, left=left)
    scope.shared = _walk(root / "shared", "shared", where=label, left=left)
    return scope


def scan(source: Path) -> Plan:
    """Read an earlier workspace folder; nothing here writes or opens a database."""
    root = source.expanduser()
    if root.is_symlink() or not root.is_dir():
        raise Unreadable("That path is not a folder CORSIA can read.")
    root = root.resolve(strict=True)
    left: list[LeftBehind] = []
    plan = Plan(root, _scope(root, WORKSPACE, context=False, left=left), left_behind=left)
    projects = root / "projects"
    if projects.is_dir() and not projects.is_symlink():
        for entry in _entries(projects):
            reason = _screened(entry.name) or (LINK if entry.is_symlink() else None)
            if reason is not None:
                left.append(LeftBehind(WORKSPACE, f"projects/{entry.name}", reason))
                continue
            if not entry.is_dir():
                continue
            scope = _scope(entry, entry.name, context=True, left=left)
            scope.name = project_name(scope.context, entry.name)
            plan.projects.append(scope)
    if not plan.projects and not plan.workspace.skills and not plan.workspace.shared:
        raise Unreadable(
            "Nothing to bring over: that folder has no projects, skills or shared documents."
        )
    return plan
