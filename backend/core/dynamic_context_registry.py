"""Shared CTX source provider registry.

This module is the single collection path for context sources that need to be
visible in preview/proof snapshots. Providers own their content rows; CTX owns
the ordering and snapshot freezing after collection.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.effective_context import ContextSourceEntry
from core.installed_context import InstalledDelivery

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextSourceCollection:
    """Resolved context-source rows plus shared lookup facts."""

    sources: list[ContextSourceEntry]
    agent_facts: dict[str, Any]
    project_cwd: str | None
    # Private: the static rows delivered and their boundary (never published).
    installed: InstalledDelivery | None = None

def provider_context_prompt(
    sources: list[ContextSourceEntry] | tuple[ContextSourceEntry, ...],
    *,
    exclude_source_ids: set[str] | None = None,
) -> str:
    """Return provider-deliverable text for included source prompt content."""
    excluded = exclude_source_ids or set()
    chunks: list[str] = []
    for source in sources:
        if source.source_id in excluded:
            continue
        if source.inclusion not in {"included", "summarized"}:
            continue
        if not source.prompt_content:
            continue
        chunks.append(
            f"[{source.origin_section}] {source.label}\n{source.prompt_content}"
        )
    return "\n\n".join(chunks)


def _table_exists(db, table: str) -> bool:
    if db is None:
        return False
    try:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _thread_context_dir_entries(
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    db,
) -> list[str]:
    """Dir-as-truth: ``.threads/{id}/context/*.md`` auto-load as thread bootloader.

    Mirrors the system/project ``context/`` dir-scan (Phase 1a) at thread scope.
    Returns absolute-path strings so the loader treats them like any other entry.
    """
    from core.workspace_context_sources import _context_dir_files
    from core.workspace_layout import (
        resolve_project_root,
        resolve_thread_root,
        resolve_workspace_root,
    )

    scope_root = (
        resolve_project_root(workspace, project_id, db=db)
        if project_id
        else resolve_workspace_root(workspace)
    )
    if scope_root is None:
        return []
    thread_root = resolve_thread_root(scope_root, thread_id)
    return [str((thread_root / rel).resolve(strict=False)) for rel in _context_dir_files(thread_root)]


def _thread_bootloader_sources(
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    db,
) -> list[ContextSourceEntry]:
    from core.bootloader_management import thread_bootloader_files
    from core.workspace_layout import resolve_project_root, resolve_workspace_root

    scope_root = (
        resolve_project_root(workspace, project_id, db=db)
        if project_id
        else resolve_workspace_root(workspace)
    )
    if scope_root is None:
        return []

    configured = thread_bootloader_files(thread_id, db=db)
    seen_names = {Path(f).name for f in configured}
    entries_list = list(configured)
    for extra in _thread_context_dir_entries(thread_id, project_id, workspace, db):
        if Path(extra).name not in seen_names:
            entries_list.append(extra)
            seen_names.add(Path(extra).name)

    entries: list[ContextSourceEntry] = []
    for entry in entries_list:
        p = Path(entry).expanduser()
        resolved = p.resolve(strict=False) if p.is_absolute() else (workspace / p).resolve(strict=False)
        exists = resolved.is_file()
        content = None
        if exists:
            try:
                content = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                exists = False
        entries.append(
            ContextSourceEntry(
                source_id=f"thread-bootloader:{thread_id}:{entry}",
                origin_section="thread",
                label=f"Thread bootloader: {Path(entry).name}",
                stable_reference=str(resolved) if exists else f"thread://{thread_id}/bootloader/{entry}",
                freshness="fresh" if exists else "missing",
                inclusion="included" if exists else "omitted",
                reason="thread bootloader file" if exists else "thread bootloader file missing",
                owner_surface="thread-settings" if not exists else "document-viewer",
                prompt_content=f"## {Path(entry).name}\n\n{content}" if content else None,
                metadata={
                    "thread_id": thread_id,
                    "path": str(resolved),
                    "stored_path": entry,
                    "repair_owner": "thread-settings",
                },
            )
        )
    return entries


def _role_sources(
    thread_id: str, project_id: str | None, workspace: Path, db,
):
    """Resolve role sources once and preserve their declared CTX order."""
    from core.conductor_templates import conductor_template_sources
    from core.role_context_sources import resolve_role_context

    role_context = resolve_role_context(
        thread_id=thread_id,
        project_id=project_id,
        workspace=workspace,
        db=db,
        strict_db=True,
    )
    by_role = {
        role: [source for source in role_context.sources
               if source.metadata.get("role") == role]
        for role in ("agent", "conductor", "corsia_agent")
    }
    errors = [
        source for source in role_context.sources
        if source.metadata.get("role") not in by_role
    ]
    ordered = [
        *by_role["agent"],
        *conductor_template_sources(
            thread_id, db=db, scopes=role_context.coordination_scopes,
        ),
        *by_role["conductor"],
        *by_role["corsia_agent"],
        *errors,
    ]
    return ordered, role_context


def _static_sources(
    thread_id: str, project_id: str | None, workspace: Path, db,
):
    """Read the static sources from disk now, in CTX order."""
    from core.project_context import project_context_source_entries_and_cwd
    from core.skills_registry import skill_context_sources
    from core.workspace_context_sources import workspace_context_source_entries

    role_sources, role_context = _role_sources(
        thread_id, project_id, workspace, db,
    )
    from core.system_tier import system_tier_sources

    sources: list[ContextSourceEntry] = []
    sources.extend(system_tier_sources(role_context.roles, workspace=workspace))
    sources.extend(workspace_context_source_entries(workspace, db=db))

    project_cwd = None
    if project_id:
        try:
            project_sources, project_cwd = project_context_source_entries_and_cwd(
                workspace,
                project_id,
                db=db,
            )
        except (RuntimeError, sqlite3.Error, OSError) as exc:
            raise RuntimeError(
                f"project source lookup failed for {thread_id}: {exc}"
            ) from exc
        sources.extend(project_sources)

    sources.extend(role_sources)

    sources.extend(_thread_bootloader_sources(thread_id, project_id, workspace, db))
    sources.extend(
        skill_context_sources(
            thread_id=thread_id,
            project_id=project_id,
            workspace=workspace,
            db=db,
            role_context=role_context,
        )
    )
    from core.guidance_manifest import stamp_raw_versions

    # The one fresh disk read: installed rows keep these through a checkpoint.
    stamp_raw_versions(sources)
    return sources, project_cwd, role_context


def _project_cwd(project_id: str | None, workspace: Path, db) -> str | None:
    """Resolve the provider cwd without reading any project context file."""
    from core.project_context import resolve_project_paths

    if not project_id:
        return None
    project_name, workspace_dir = resolve_project_paths(
        workspace, project_id, db=db,
    )
    return str(workspace_dir) if project_name and workspace_dir else None


def collect_context_sources(
    *,
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    db,
    selected_model: str | None = None,
) -> ContextSourceCollection:
    """Collect installed static sources and dynamic provider rows in CTX order.

    Static sources come from the thread's install boundary when it retains
    them for this execution, reading no context file; otherwise from disk
    now. ``installed`` names what was delivered so the dispatch can retain it
    (``core.installed_context``); collection itself never writes.
    """
    from core.agent_context_sources import installed_owner_map
    from core.installed_context import (
        ADOPTION_RETAINED, ROLE_FACTS, read_boundary, restore_sources,
        serialize_sources,
    )

    try:
        boundary_id, stored = (
            read_boundary(thread_id, db) if db is not None else (None, None)
        )
    except sqlite3.Error as exc:
        # The owner map reports the same schema gap as an omitted row.
        log.warning("installed boundary unreadable for %s: %s", thread_id, exc)
        boundary_id, stored = None, None
    owner_map, map_snapshot, adoption = installed_owner_map(
        thread_id=thread_id, db=db, stored=stored, selected_model=selected_model,
    )
    if adoption == ADOPTION_RETAINED and stored is not None:
        rows, role_facts = stored["sources"], stored[ROLE_FACTS]
        sources = restore_sources(rows)
        project_cwd = _project_cwd(project_id, workspace, db)
    else:
        sources, project_cwd, role_context = _static_sources(
            thread_id, project_id, workspace, db,
        )
        rows, role_facts = serialize_sources(sources), role_context.facts()
    installed = None
    if map_snapshot is not None:
        installed = InstalledDelivery(
            thread_id=thread_id, adoption=adoption, boundary_id=boundary_id,
            stored=stored,
            snapshot={**map_snapshot, "sources": rows, ROLE_FACTS: role_facts},
        )
    sources.append(owner_map)
    agent_facts = {
        "agent_count": owner_map.metadata.get("agent_count", 0),
        "agent_thread_ids": owner_map.metadata.get("agent_thread_ids", []),
        "responsibility_map_snapshot": (
            None if map_snapshot is None
            else {**map_snapshot, ROLE_FACTS: role_facts}
        ),
        **role_facts,
    }
    return ContextSourceCollection(
        sources=sources,
        agent_facts=agent_facts,
        project_cwd=project_cwd,
        installed=installed,
    )
