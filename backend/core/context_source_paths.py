"""Answer which scope loads a written path, by asking the loaders themselves.

Task 3's rule is that a changed bootloader path becomes one coalesced passive
notice on the thread's next independent activity. Detection belongs wherever a
write is already observed, so this module answers one question and holds no
state: *which scope loads this path, if any?*

**The configured list is the authority, not the directory shape.** A scope's
effective sources are a configured list plus a directory scan
(`workspace_context_sources.workspace_context_bootloader_files` for workspace,
`bootloader_management.thread_bootloader_files` plus the tray scan for a
thread), and the configured half may point anywhere the bootloader API allows —
including outside `context/`. Deriving eligibility from the path shape alone
would silently miss exactly those files, which are live prefix material. So
this module calls the same functions the loaders call and treats membership as
proof. Shape is used only where the directory genuinely *is* the rule: project
context and role directories.

Nothing here persists an accepted-version ledger, re-hashes every source on
every turn, or compares a delivered prefix. A change is journaled once, by the
same path every other passive producer uses.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_CONTEXT_DIR = "context"
_ROLES_DIR = "roles"
_THREADS_DIR = ".threads"
_PROJECTS_DIR = "projects"
_SUFFIX = ".md"


@dataclass(frozen=True, slots=True)
class ContextSourceChange:
    """One changed context source, addressed the way the journal addresses it."""

    object_kind: str
    object_id: str
    label: str


def _absolute(path: str | Path) -> Path | None:
    """Normalize lexically; a relative path names no scope and is refused."""
    candidate = Path(os.path.expanduser(str(path)))
    if not candidate.is_absolute():
        return None
    return Path(os.path.abspath(candidate))


def _same_file(left: Path, right: Path) -> bool:
    """Compare two paths for identity, tolerating symlinked scope roots."""
    if left == right:
        return True
    return left.resolve(strict=False) == right.resolve(strict=False)


def _label_for(path: Path, workspace: Path) -> str:
    """Name the changed file the way a reader would recognize it."""
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.name


def role_file_label(path: Path, scope_root: Path) -> str | None:
    """Return the label when ``path`` is a role file the loader actually reads.

    Role directories are directory-backed, but only the roles in
    ``role_context_sources.ROLE_ORDER`` are ever delivered; a file under any
    other role name would be a notice about something nobody loads.
    """
    from core.role_context_sources import ROLE_ORDER

    try:
        relative = path.relative_to(scope_root)
    except ValueError:
        return None
    parts = relative.parts
    if path.suffix != _SUFFIX or len(parts) != 4:
        return None
    if parts[0] != _CONTEXT_DIR or parts[1] != _ROLES_DIR:
        return None
    if parts[2] not in ROLE_ORDER:
        return None
    return relative.as_posix()


def _scope_context_label(path: Path, scope_root: Path) -> str | None:
    """Return the label for a directory-backed ``context/<name>.md`` file."""
    try:
        relative = path.relative_to(scope_root)
    except ValueError:
        return None
    parts = relative.parts
    if path.suffix != _SUFFIX or len(parts) != 2 or parts[0] != _CONTEXT_DIR:
        return None
    return relative.as_posix()


def _workspace_change(path: Path, workspace: Path, db) -> ContextSourceChange | None:
    """Match against the loader's own effective workspace list."""
    from core.workspace_context_sources import (
        workspace_context_bootloader_files,
        workspace_context_path,
    )

    for filename in workspace_context_bootloader_files(db=db, workspace=workspace):
        if _same_file(workspace_context_path(workspace, filename), path):
            return ContextSourceChange(
                "workspace_context", "workspace", _label_for(path, workspace)
            )
    role_label = role_file_label(path, workspace)
    if role_label is not None:
        return ContextSourceChange("workspace_context", "workspace", role_label)
    return None


def _thread_candidates(path: Path, db) -> list[str]:
    """Threads that might load ``path``: its own tray, plus any that configured it.

    The reverse lookup is filtered by basename so it stays one bounded query
    over the threads that have a configured list at all. ``bootloader_files``
    is stored ``json.dumps``-escaped, so the pattern is the JSON encoding of
    the basename, not the raw name: ``café.md`` is ``caf\\u00e9.md`` on disk.
    """
    candidates: list[str] = []
    parts = path.parts
    for index in range(len(parts) - 1, 0, -1):
        if parts[index - 1] == _THREADS_DIR:
            candidates.append(parts[index])
            break
    encoded_name = json.dumps(path.name)[1:-1]
    rows = db.execute(
        "SELECT id FROM threads WHERE bootloader_files IS NOT NULL "
        "AND bootloader_files LIKE ?",
        (f"%{encoded_name}%",),
    ).fetchall()
    candidates.extend(
        str(row["id"]) for row in rows if str(row["id"]) not in candidates
    )
    return candidates


def _thread_changes(
    path: Path, workspace: Path, db
) -> list[tuple[str | None, ContextSourceChange]]:
    """Every thread that actually loads ``path``, with the project each belongs to.

    A path merely *sitting* in `.threads/<id>/` is not proof: the thread's own
    effective list is, which is why the tray only nominates a candidate. Every
    confirmed loader is returned, not the first, because each one would
    otherwise keep stale instructions.
    """
    from core.bootloader_management import thread_bootloader_files

    changes: list[tuple[str | None, ContextSourceChange]] = []
    for thread_id in _thread_candidates(path, db):
        project_id, tray = _thread_scope(thread_id, workspace, db)
        change = _thread_change_for(
            thread_id, path, workspace, tray, thread_bootloader_files(thread_id, db=db)
        )
        if change is not None:
            changes.append((project_id, change))
    return changes


def _thread_change_for(
    thread_id: str,
    path: Path,
    workspace: Path,
    tray: Path | None,
    entries: list[str],
) -> ContextSourceChange | None:
    """Confirm one candidate thread loads ``path`` by its list or its own tray."""
    for entry in entries:
        if _same_file(_entry_path(entry, workspace), path):
            return ContextSourceChange(
                "thread_context", thread_id, _label_for(path, workspace)
            )
    # The tray is resolved from the thread's own scope, so a path that merely
    # looks like a tray, inside a registered repository, say, cannot match.
    if tray is None:
        return None
    label = _scope_context_label(path, tray)
    if label is None:
        return None
    return ContextSourceChange("thread_context", thread_id, label)


def _entry_path(entry: str, workspace: Path) -> Path:
    """Resolve one configured-list entry the way the loaders do."""
    path = Path(entry).expanduser()
    return path if path.is_absolute() else workspace / path


def configured_source_paths(workspace: Path, db) -> set[Path]:
    """Every path a configured list loads: the workspace list and live threads' lists."""
    from core.bootloader_management import _json_list
    from core.workspace_context_sources import (
        workspace_context_bootloader_files,
        workspace_context_path,
    )

    paths = {
        workspace_context_path(workspace, name)
        for name in workspace_context_bootloader_files(db=db, workspace=workspace)
    }
    rows = db.execute(
        "SELECT bootloader_files FROM threads "
        "WHERE bootloader_files IS NOT NULL AND archived_at IS NULL"
    ).fetchall()
    for row in rows:
        for entry in _json_list(row["bootloader_files"]) or []:
            paths.add(_entry_path(entry, workspace))
    return paths


def _thread_scope(thread_id: str, workspace: Path, db) -> tuple[str | None, Path | None]:
    """Ask the loader where this thread's tray is, rather than inferring it.

    Returns the thread's project id and its tray. The tray is
    ``<scope_root>/.threads/<id>`` for the thread's *own* scope, so a lexical
    scan for `.threads` is not equivalent: a registered repository lives under
    ``projects/<slug>/source/``, inside the workspace, and may contain its own
    `.threads/<id>/context/` that CORSIA never loads.
    """
    from core.workspace_layout import (
        resolve_project_root,
        resolve_thread_root,
        resolve_workspace_root,
    )

    row = db.execute(
        "SELECT project_id FROM threads WHERE id = ?", (thread_id,)
    ).fetchone()
    if row is None:
        return None, None
    project_id = row["project_id"]
    scope_root = (
        resolve_project_root(workspace, project_id, db=db)
        if project_id
        else resolve_workspace_root(workspace)
    )
    if scope_root is None:
        return project_id, None
    try:
        return project_id, resolve_thread_root(scope_root, thread_id)
    except (ValueError, OSError):
        # A rejected layout (symlinked or escaping tray) is a definite "no
        # valid tray for this candidate", not a swallowed journal failure.
        return project_id, None


def _project_change(path: Path, workspace: Path, db) -> ContextSourceChange | None:
    """Project context is directory-backed, so shape is the rule here.

    The label is resolved lexically before the project id is looked up, so an
    ordinary write under ``projects/`` costs no database work at all.
    """
    projects_root = workspace / _PROJECTS_DIR
    try:
        relative = path.relative_to(projects_root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    project_root = projects_root / relative.parts[0]
    label = _scope_context_label(path, project_root) or role_file_label(path, project_root)
    if label is None:
        return None
    project_id = project_id_for_root(project_root, workspace, db)
    if project_id is None:
        return None
    return ContextSourceChange("project_context", project_id, label)


def project_id_for_root(root: Path, workspace: Path, db) -> str | None:
    """Resolve a planning directory back to the project that owns it.

    Both sides are resolved for this comparison only. Project roots are plain
    directories, so following symlinks here is safe and it keeps a resolved
    stored root (``/private/var`` on macOS) matching a lexical one.
    """
    from core.project_context import resolve_project_paths
    from infra.workspace_paths import reference_leaf

    # A database failure is not converted to "no owning project" here: it
    # propagates to append_context_source_change, which owns the single
    # best-effort policy for this path. Two handlers would mean two policies.
    #
    # A logical project path ends in the planning directory name, so the stored
    # path picks the candidate rows worth confirming. `resolve_project_paths`
    # stays the authority for that confirmation; it is just no longer called
    # once per project row.
    rows = db.execute("SELECT id, path FROM projects").fetchall()
    target = root.resolve(strict=False)
    for row in rows:
        if reference_leaf(row["path"]) != root.name:
            continue
        project_id = str(row["id"])
        try:
            _name, planning_root = resolve_project_paths(
                workspace,
                project_id,
                db=db,
            )
        except (ValueError, RuntimeError, OSError):
            continue
        if planning_root is not None and planning_root.resolve(strict=False) == target:
            return project_id
    return None


def classify_context_source(
    path: str | Path,
    *,
    workspace: str | Path,
    db,
) -> tuple[ContextSourceChange, ...]:
    """Return every journal address that loads ``path``; empty for an ordinary file.

    A file can be a workspace or project source *and* be configured by threads,
    and several threads can configure one file. Each loader gets its own row.
    A thread row is dropped only when a wider row already reaches that thread:
    a workspace row reaches every thread, and a project row reaches the threads of
    that project. Threads outside that scope keep their own row.
    """
    absolute = _absolute(path)
    if absolute is None:
        return ()
    workspace_root = _absolute(workspace)
    if workspace_root is None:
        return ()

    changes: list[ContextSourceChange] = []
    workspace_change = _workspace_change(absolute, workspace_root, db)
    if workspace_change is not None:
        changes.append(workspace_change)
    project_change = _project_change(absolute, workspace_root, db)
    if project_change is not None:
        changes.append(project_change)
    for project_id, thread_change in _thread_changes(absolute, workspace_root, db):
        if workspace_change is not None:
            continue
        if project_change is not None and project_id == project_change.object_id:
            continue
        changes.append(thread_change)
    return tuple(changes)


def append_context_source_change(
    db,
    *,
    path: str,
    workspace: str | Path | None = None,
    actor_thread_id: str | None,
) -> int | None:
    """Journal one bootloader-source change per loader, whoever wrote the file.

    Returns the last journal sequence appended, or ``None`` for an ordinary
    path. Pass ``None`` to own the standalone notice transaction, or a named
    connection to preserve its enclosing transaction. Filesystem classification
    precedes the journal write unit; all recipient rows succeed or roll back
    together.

    Classification and journal failures propagate so the file-save completion
    owner can expose its partial result instead of misreporting the failed
    notice as an ordinary non-source.
    """
    from infra.database import get_db, write_transaction
    from infra.passive_changes import append_change

    if workspace is None:
        from infra.config import WORKSPACE

        workspace = WORKSPACE
    read_db = db if db is not None else get_db()
    changes = classify_context_source(path, workspace=workspace, db=read_db)
    if not changes:
        return None
    sequence: int | None = None
    with write_transaction(db, scope="context_source_change") as connection:
        for change in changes:
            sequence = append_change(
                connection,
                object_kind=change.object_kind,
                object_id=change.object_id,
                change_kind="updated",
                summary=f"Context source updated: {change.label}",
                details={"path": change.label},
                actor_thread_id=actor_thread_id,
            )
    return sequence
