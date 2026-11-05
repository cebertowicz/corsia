"""Workspace bootstrap context source helpers."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from core.effective_context import ContextSourceEntry

# Directory-as-truth: static workspace bootloader files live in the workspace
# ``context/`` directory and are discovered by the directory scan. This remains
# the fallback for a missing ``workspace_context`` setting row.
WORKSPACE_CONTEXT_FILES: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceContextFile:
    """One canonical file consumed by the workspace bootstrap layer."""

    source_id: str
    name: str
    label: str
    path: Path
    exists: bool
    editable: bool
    owner_surface: str
    size: int | None = None
    mtime: float | None = None

    def to_dict(self) -> dict:
        """Return the API-safe file inventory projection."""
        return {
            "source_id": self.source_id,
            "name": self.name,
            "label": self.label,
            "path": str(self.path),
            "exists": self.exists,
            "editable": self.editable,
            "owner_surface": self.owner_surface,
            "size": self.size,
            "mtime": self.mtime,
        }


def _context_dir_files(scope_root: Path) -> list[str]:
    """Return sorted ``context/*.md`` paths found under a scope root."""
    ctx_dir = scope_root / "context"
    if not ctx_dir.is_dir():
        return []
    return sorted(
        f"context/{path.name}"
        for path in ctx_dir.iterdir()
        if path.is_file() and path.suffix == ".md"
    )


def workspace_context_path(workspace: Path, filename: str) -> Path:
    """Return the canonical path for a workspace bootstrap file."""
    path = Path(filename).expanduser()
    if path.is_absolute():
        return path.resolve(strict=False)
    return (workspace / filename).expanduser().resolve()


def _is_runtime_workspace(workspace: Path | None) -> bool:
    if workspace is None:
        return True
    from infra.config import WORKSPACE

    try:
        return workspace.expanduser().resolve(strict=False) == WORKSPACE.expanduser().resolve(strict=False)
    except OSError:
        return False


def _setting_value(*, db=None, workspace: Path | None = None) -> dict:
    """Read the workspace bootloader setting."""
    if db is not None:
        try:
            row = db.execute("SELECT value FROM settings WHERE key = 'workspace_context'").fetchone()
            raw = row[0] if row else None
            parsed = json.loads(raw) if raw else None
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if _is_runtime_workspace(workspace):
        try:
            from infra.settings import get_setting

            setting = get_setting("workspace_context")
        except (sqlite3.Error, OSError, RuntimeError):
            return {}
        return setting if isinstance(setting, dict) else {}
    return {}


def workspace_context_configured_files(*, db=None, workspace: Path | None = None) -> list[str]:
    """Return the literal configured workspace bootloader list without a scan."""
    configured = _setting_value(db=db, workspace=workspace).get("bootloader_files")
    if isinstance(configured, list):
        return [str(item) for item in configured]
    return list(WORKSPACE_CONTEXT_FILES)


def workspace_context_bootloader_files(*, db=None, workspace: Path | None = None) -> list[str]:
    """Return configured workspace files plus directory-discovered files."""
    files = workspace_context_configured_files(db=db, workspace=workspace)
    if workspace is not None:
        seen_names = {Path(filename).name for filename in files}
        for extra in _context_dir_files(workspace):
            if Path(extra).name not in seen_names:
                files.append(extra)
                seen_names.add(Path(extra).name)
    return files


def workspace_context_files(workspace: Path, *, db=None) -> list[WorkspaceContextFile]:
    """Return the canonical workspace bootstrap file inventory."""
    rows: list[WorkspaceContextFile] = []
    for filename in workspace_context_bootloader_files(db=db, workspace=workspace):
        path = workspace_context_path(workspace, filename)
        exists = path.is_file()
        stat = path.stat() if exists else None
        rows.append(
            WorkspaceContextFile(
                source_id=f"workspace-context:{filename}",
                name=filename,
                label=filename,
                path=path,
                exists=exists,
                editable=False,
                owner_surface="document-viewer" if exists else "workspace-settings",
                size=stat.st_size if stat else None,
                mtime=stat.st_mtime if stat else None,
            )
        )
    return rows


def is_read_only_context_path(path: Path, workspace: Path, *, db=None) -> bool:
    """Whether every scope reads ``path`` and the doc API never saves it.

    True for a canonical workspace bootstrap file and for a generated System
    projection file.
    """
    from core.system_projection import is_system_projection_path

    if is_system_projection_path(path, workspace):
        return True
    try:
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        return False
    return any(resolved == row.path for row in workspace_context_files(workspace, db=db))


def workspace_context_source_entries(workspace: Path, *, db=None) -> list[ContextSourceEntry]:
    """Return CTX source rows for the workspace bootstrap file layer."""
    entries: list[ContextSourceEntry] = []
    for row in workspace_context_files(workspace, db=db):
        content = row.path.read_text(encoding="utf-8") if row.exists else None
        entries.append(
            ContextSourceEntry(
                source_id=row.source_id,
                origin_section="workspace",
                label=f"Workspace context: {row.name}",
                stable_reference=str(row.path) if row.exists else f"workspace://context/{row.name}",
                freshness="fresh" if row.exists else "missing",
                inclusion="included" if row.exists else "omitted",
                reason="workspace context file" if row.exists else "workspace context file missing",
                owner_surface=row.owner_surface,
                prompt_content=f"## {row.name}\n\n{content}" if content is not None else None,
                metadata={
                    "name": row.name,
                    "path": str(row.path),
                    "editable": row.editable,
                    "exists": row.exists,
                    "size": row.size,
                    "mtime": row.mtime,
                },
            )
        )
    return entries
