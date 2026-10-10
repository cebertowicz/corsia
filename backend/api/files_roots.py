"""Root discovery and tree helpers for the files API."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from doceditor.document_scope import (
    ProjectNotFound,
    resolve_project as _resolve_project,
    scope_bases as _scope_bases,
)
from infra import config
from infra.workspace_paths import looks_like_workspace_reference
from registry import service as registry_service
from registry.doc_freshness import freshness
from registry.identity_metadata import parse_metadata
from registry.shared_principals import is_under_shared_roots, owner_label, shared_roots

MAX_NODES = 2000
_PRUNE_DIRS = {
    "node_modules", "__pycache__", "dist", "build", ".venv", "venv",
    ".worktrees", ".git",
}


def resolve_project(project_id: str, db) -> tuple[Path | None, list[tuple[str, Path]]]:
    try:
        return _resolve_project(project_id, db)
    except ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


def scope_bases(project_id: str | None, db) -> list[Path]:
    try:
        return _scope_bases(project_id, db)
    except ProjectNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


def walk(root: Path) -> tuple[list[dict], bool]:
    nodes: list[dict] = []
    capped = False
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        dirnames[:] = sorted(
            name for name in dirnames
            if not name.startswith(".") and name.lower() not in _PRUNE_DIRS
        )
        if directory == root and root == config.WORKSPACE.resolve() and "projects" in dirnames:
            dirnames.remove("projects")
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = directory / name
            rel = path.relative_to(root)
            try:
                stat = path.stat()
            except OSError:
                continue
            nodes.append({
                "rel": str(rel),
                "name": name,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "tmp": registry_service.is_tmp(rel),
            })
            if len(nodes) >= MAX_NODES:
                capped = True
                return nodes, capped
    return nodes, capped


def artifact_index(db) -> dict:
    rows = db.execute(
        """SELECT i.id, i.external_id, i.path, i.description, i.metadata,
                  r.parent_thread_id, i.updated_at,
                  s.title AS owner_title
           FROM artifact_identities i
           JOIN artifact_registrations r ON r.artifact_id=i.id
           LEFT JOIN threads s ON s.id=r.parent_thread_id
           WHERE i.integration='workspace' AND i.state='active'
             AND r.relationship_state IN ('owner-pinned', 'owner-active')"""
    ).fetchall()
    by_path: dict[str, Any] = {}
    ranks: dict[str, tuple[bool, str, str]] = {}
    for raw in rows:
        row = dict(raw)
        path = row["path"] or row["external_id"]
        rank = (
            looks_like_workspace_reference(row["external_id"]),
            row["updated_at"],
            row["id"],
        )
        if path not in ranks or rank > ranks[path]:
            by_path[path] = row
            ranks[path] = rank
    return by_path


def shared_projector(db) -> Callable[[str, dict], None]:
    """Add owner name and freshness to a shared-document row, on demand.

    Resolving every indexed path costs ~8 syscalls each, so the tree pays
    it only for rows its walk matched. Roots and owner names resolve once.
    """
    roots = shared_roots(workspace=config.WORKSPACE, db=db)
    labels: dict[str, str] = {}
    seen: set[str] = set()
    now = datetime.now(timezone.utc)

    def project(path: str, row: dict) -> None:
        if path in seen:
            return
        seen.add(path)
        if not is_under_shared_roots(path, roots):
            return
        owner = row["parent_thread_id"]
        if owner not in labels:
            labels[owner] = owner_label(owner, db=db)
        row["owner_name"] = labels[owner]
        row["freshness"] = freshness(path, parse_metadata(row["metadata"]), now)

    return project


def annotate(
    nodes: list[dict],
    root: Path,
    by_path: dict,
    project: Callable[[str, dict], None],
) -> None:
    for node in nodes:
        path = str(root / node["rel"])
        row = by_path.get(path)
        if row:
            project(path, row)
            node["artifact"] = {
                "id": row["id"],
                "description": row["description"],
                "owner_thread_id": row["parent_thread_id"],
                "owner_thread": row["owner_title"],
                "updated_at": row["updated_at"],
            }
            if "freshness" in row:
                node["owner_name"] = row["owner_name"]
                node["freshness"] = {"state": row["freshness"]["state"]}
