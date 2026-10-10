"""Project-scoped context loading for the context builder."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from core.registered_repos import registered_repo_context_lines
from core.workspace_context_sources import _context_dir_files
from infra.platform_paths import WorkspaceSelection
from infra.workspace_paths import (
    WorkspaceReference,
    looks_like_workspace_reference,
    workspace_selection_for_root,
)


@dataclass(frozen=True)
class LoadedProjectContextFile:
    filename: str
    path: Path
    content: str


REQUIRED_PROJECT_CONTEXT_FILES = (
    "context/PROJECT.md",
    "context/COORDINATION-MODEL.md",
)


def _project_context_order(filename: str) -> tuple[int, str]:
    """Order project identity and intent before procedural coordination."""
    name = Path(filename).name.upper()
    if name == "PROJECT.MD":
        priority = 0
    elif name.startswith("VISION") or name.startswith("INTENT"):
        priority = 1
    elif name.startswith("SAFETY") or "INVARIANT" in name:
        priority = 2
    elif name == "COORDINATION-MODEL.MD":
        priority = 4
    else:
        priority = 3
    return priority, filename.casefold()


def project_context_filenames(scope_root: Path) -> list[str]:
    """Return directory-backed project context in canonical authority order."""
    filenames = _context_dir_files(scope_root)
    missing = [
        filename for filename in REQUIRED_PROJECT_CONTEXT_FILES if filename not in filenames
    ]
    if missing:
        raise RuntimeError(
            "missing required project context: " + ", ".join(missing)
        )
    return sorted(filenames, key=_project_context_order)


def _key(row: Any, key: str) -> Any:
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key)
    return None


def load_project_context(
    workspace: Path,
    project_id: str,
    *,
    db=None,
    file_parts: list[str] | None = None,
) -> str | None:
    """Load project context; ``file_parts`` replaces the disk read with
    installed ``## filename`` bytes while header lines stay live settings."""
    if file_parts is None:
        project_name, workspace_dir, files = (
            _loaded_project_context_files(workspace, project_id, db=db))
        file_parts = [f"## {f.filename}\n\n{f.content}" for f in files]
    else:
        project_name, workspace_dir = resolve_project_paths(
            workspace, project_id, db=db)
    if not project_name:
        return None

    parts = [f"# Project: {project_name}"]
    if workspace_dir:
        parts.append(f"Project workspace: {workspace_dir}")

    parts.extend(registered_repo_context_lines(project_id, db=db))
    parts.extend(f"\n{part}" for part in file_parts)

    return "\n".join(parts) if file_parts else None


def _source_entries_from_loaded(project_id: str, project_name: str, loaded_files: list):
    from core.effective_context import ContextSourceEntry

    entries: list[ContextSourceEntry] = []
    for loaded in loaded_files:
        resolved = loaded.path.resolve()
        entries.append(
            ContextSourceEntry(
                source_id=f"project-bootloader:{project_id}:{loaded.filename}",
                origin_section="project",
                label=f"Project bootloader: {loaded.filename}",
                stable_reference=str(resolved),
                freshness="fresh",
                inclusion="included",
                reason="project bootloader file",
                owner_surface="document-viewer",
                prompt_content=f"## {loaded.filename}\n\n{loaded.content}",
                metadata={
                    "project_id": project_id,
                    "project_name": project_name,
                    "relative_path": loaded.filename,
                    "path": str(resolved),
                },
            )
        )
    return entries


def project_context_source_entries(workspace: Path, project_id: str, *, db=None):
    """Return effective-context source rows for loaded project bootloader files."""
    project_name, _workspace_dir, loaded_files = _loaded_project_context_files(
        workspace,
        project_id,
        db=db,
    )
    if not project_name:
        return []
    return _source_entries_from_loaded(project_id, project_name, loaded_files)


def project_context_source_entries_and_cwd(workspace: Path, project_id: str, *, db=None):
    """Return project source rows and provider cwd from one project-path lookup."""
    project_name, workspace_dir, loaded_files = _loaded_project_context_files(
        workspace,
        project_id,
        db=db,
    )
    if not project_name:
        return [], None
    return (
        _source_entries_from_loaded(project_id, project_name, loaded_files),
        str(workspace_dir) if workspace_dir else None,
    )


def resolve_project_paths(
    workspace: Path,
    project_id: str,
    *,
    db=None,
    selection: WorkspaceSelection | None = None,
) -> tuple[str | None, Path | None]:
    """Resolve project name and selected planning root.

    A logical workspace reference resolves to its planning root. A legacy
    row the startup converter has kept (its checkout is not yet reachable)
    resolves to the selected project folder it names, if any. A
    selected-root mismatch for a logical reference still raises.
    """
    conn = db
    should_close = False
    try:
        if conn is None:
            from infra.database import get_db
            conn = get_db()
            should_close = True
        row = conn.execute("SELECT path, name FROM projects WHERE id = ?", (project_id,)).fetchone()
    finally:
        if should_close and conn is not None:
            conn.close()

    if not row:
        return None, None
    return resolve_project_row_paths(
        workspace,
        project_name=_key(row, "name"),
        raw_path=_key(row, "path"),
        selection=selection,
    )


def resolve_project_row_paths(
    workspace: Path,
    *,
    project_name: object,
    raw_path: object,
    selection: WorkspaceSelection | None = None,
) -> tuple[str | None, Path | None]:
    """Resolve a prefetched project row without another database lookup."""
    selected_root = Path(workspace).expanduser().resolve(strict=False)
    name = project_name if isinstance(project_name, str) else None
    if not looks_like_workspace_reference(raw_path):
        return name, legacy_planning_root(selected_root, name, raw_path)
    selected = selection or workspace_selection_for_root(selected_root)
    if selected.root != selected_root:
        raise ValueError(
            "logical project selected root does not match workspace argument: "
            f"{selected.root} != {selected_root}"
        )
    planning_root = _resolve_logical_project_root(
        WorkspaceReference.parse(str(raw_path)),
        selected,
    )
    return name, planning_root


def _resolve_logical_project_root(
    reference: WorkspaceReference,
    selection: WorkspaceSelection,
) -> Path:
    """Resolve one logical project ref as a direct non-symlink project child."""
    if len(reference.parts) != 2 or reference.parts[0] != "projects":
        raise ValueError(
            "logical project reference must name a direct child of projects/"
        )
    projects_root = selection.root / "projects"
    lexical_root = projects_root / reference.parts[1]
    if lexical_root.is_symlink():
        raise ValueError(f"logical project root cannot be a symlink: {lexical_root}")
    resolved = reference.resolve(selection)
    if resolved.parent != projects_root:
        raise ValueError(
            "logical project reference must resolve as a direct child of projects/"
        )
    return resolved


def project_slug(name: object) -> str | None:
    """Return the project-directory slug for a project name."""
    if not isinstance(name, str) or not name.strip():
        return None
    slug = name.strip().lower().replace(" ", "-")
    if Path(slug).name != slug or slug in {".", ".."}:
        return None
    return slug


def _existing_selected_project_dir(candidate: Path, selected_root: Path) -> Path | None:
    """Return an existing nonsymlink project candidate inside the selection."""
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(selected_root):
        return None
    if candidate.is_symlink() or not candidate.is_dir():
        return None
    return resolved


def legacy_planning_root(
    selected_root: Path,
    project_name: object,
    raw_path: object,
) -> Path | None:
    """Return the selected project folder a legacy (non-logical) row names.

    A stored path directly inside ``projects/`` is that folder; otherwise a
    stored ``.../projects/<name>`` path names ``projects/<name>``, and the
    project-name slug is the fallback. Only existing folders count. Remove
    together with the startup converter (``core.project_path_conversion``).
    """
    projects_root = selected_root / "projects"
    slug = project_slug(project_name)
    candidates: list[Path] = []
    if not raw_path:
        if slug:
            candidates.append(projects_root / slug)
    else:
        legacy = Path(str(raw_path)).expanduser()
        if not legacy.is_absolute():
            return None
        lexical = Path(os.path.abspath(legacy))
        if lexical.parent == projects_root:
            candidates.append(lexical)
        else:
            if legacy.parent.name == "projects":
                candidates.append(projects_root / legacy.name)
            if slug and projects_root / slug not in candidates:
                candidates.append(projects_root / slug)
    return next(
        (
            resolved
            for candidate in candidates
            if (resolved := _existing_selected_project_dir(candidate, selected_root))
            is not None
        ),
        None,
    )


def _loaded_project_context_files(
    workspace: Path,
    project_id: str,
    *,
    db=None,
) -> tuple[str | None, Path | None, list[LoadedProjectContextFile]]:
    project_name, workspace_dir = resolve_project_paths(
        workspace,
        project_id,
        db=db,
    )
    if not project_name:
        return project_name, workspace_dir, []

    loaded: list[LoadedProjectContextFile] = []
    loaded_names: set[str] = set()
    # The canonical project workspace is the sole owner of project model
    # context.  Registered source repositories are code/document roots, not an
    # implicit instruction surface.
    for search_dir in [workspace_dir]:
        if not search_dir:
            continue
        for filename in project_context_filenames(search_dir):
            if filename in loaded_names:
                continue
            path = Path(filename)
            if path.is_absolute() or ".." in path.parts:
                logger.warning(f"skipping unsafe bootloader entry: {filename!r}")
                continue
            file_path = search_dir / filename
            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise RuntimeError(
                    f"failed to read required project context {file_path}: {error}"
                ) from error
            loaded.append(
                LoadedProjectContextFile(
                    filename=filename,
                    path=file_path,
                    content=content,
                )
            )
            loaded_names.add(filename)

    return project_name, workspace_dir, loaded
