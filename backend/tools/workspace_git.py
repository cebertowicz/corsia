"""Narrow agent tools for the workspace's guidance history."""

from __future__ import annotations

import os
import stat
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any

from tools.base import Tool
from tools.catalog_entry import ToolCatalogEntry
from tools.host_path_resolution import contains


WORKSPACE_GIT_CATALOG_ENTRIES = {
    "workspace_commit": ToolCatalogEntry(
        "history", status="substrate", authority="write-files",
        summary="Commit workspace files", activity="Committing workspace files",
    ),
    "workspace_history": ToolCatalogEntry(
        "history", status="substrate",
        summary="Read workspace file history", activity="Reading workspace history",
    ),
}


def _normal_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _check_path(repo: Path, path: str, fence, projects: set[str], *, history: bool) -> str:
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise ValueError("Path must be a canonical workspace-relative path.")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("Path must be a canonical workspace-relative path.")
    candidate = repo / path
    projects_root = repo / "projects"
    if (history and contains(candidate, projects_root)) or (
        _normal_name(parts[0]) == "projects" and len(parts) > 1 and _normal_name(parts[1]) not in projects
    ):
        raise ValueError("Path is not in a current project.")
    for root in fence.roots:
        for form in root.forms:
            if contains(form, candidate) or (history and contains(candidate, form)):
                raise ValueError(fence.refusal(form) or "Path is outside your scope.")
    if not history and any(_normal_name(part) == ".git" for part in parts):
        raise ValueError("Path must not contain a .git component.")
    return path


def _current_projects(repo: Path, db) -> set[str]:
    from core.project_context import resolve_project_row_paths

    result = set()
    for row in db.execute("SELECT name, path FROM projects"):
        _, root = resolve_project_row_paths(repo, project_name=row["name"], raw_path=row["path"])
        if root is not None and root.parent == repo / "projects":
            result.add(_normal_name(root.name))
    return result


def _read_file(repo: Path, path: str) -> bytes | None:
    parts = path.split("/")
    fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Path must be a regular file with one link.")
            return handle.read()
    except FileNotFoundError:
        return None
    finally:
        os.close(fd)


def _tracked_file(repo: Path, path: str) -> bool:
    from infra.guidance_git import _git

    for args in (("ls-files", "--stage", "-z"), ("ls-tree", "-z", "HEAD")):
        output = _git(repo, "--literal-pathspecs", *args, "--", path, ok=(0, 128))
        for entry in output.split("\0"):
            metadata, separator, name = entry.partition("\t")
            if separator and name == path and metadata.startswith(("100644 ", "100755 ", "120000 ")):
                return True
    return False


def _context(thread_id: str):
    from infra.config import WORKSPACE
    from infra.database import get_db
    from infra.guidance_git import workspace_repository
    from tools.project_fence import fence_for_thread

    repo, reason = workspace_repository(Path(WORKSPACE))
    if repo is None:
        raise ValueError(reason or "Workspace history is unavailable.")
    with closing(get_db()) as db:
        return repo, fence_for_thread(thread_id, db=db), _current_projects(repo, db)


def _commit(thread_id: str, paths: list[str], message: str) -> str:
    from core.guidance_paths import classify_guidance_path
    from infra.guidance_git import _acquire, _commit as commit, _git, _locked
    from infra.guidance_messages import saver_author

    repo, fence, projects = _context(thread_id)
    checked = [_check_path(repo, path, fence, projects, history=False) for path in paths]
    with _locked(_acquire(repo)[0]) as db:
        files = {}
        for path in checked:
            data = _read_file(repo, path)
            if data is None and not _tracked_file(repo, path):
                raise ValueError(f"Path is not a tracked file: {path}")
            files[path] = data
        if _git(repo, "check-ignore", "--", *checked, ok=(0, 1)).strip():
            raise ValueError("Path is ignored by Git.")
        guidance = [path for path in checked if classify_guidance_path(repo / path, workspace=repo, db=db)]
        result = commit(repo, files, message, saver_author(thread_id=thread_id), repair_paths=guidance)
    if result is None:
        return "No changes to commit."
    summary = f"Committed {result.sha}." if result.changed else "No changes to commit."
    return "\n".join([summary, *result.unsynced_line()])


def _history(thread_id: str, path: str, count: int) -> str:
    from infra.guidance_git import _git

    repo, fence, projects = _context(thread_id)
    _check_path(repo, path, fence, projects, history=True)
    return _git(repo, "--literal-pathspecs", "-c", "core.fsmonitor=false", "log", "-n", str(count), "-p", "--no-ext-diff", "--no-textconv", "--", path)


class WorkspaceCommitTool(Tool):
    @property
    def name(self) -> str:
        return "workspace_commit"

    @property
    def description(self) -> str:
        return "Commit named workspace files; refuses fenced, ignored, linked and non-regular paths."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "message": {"type": "string"}}, "required": ["paths", "message"]}

    async def execute(self, **kwargs: Any) -> str:
        from infra.db_async import run_off_db
        from infra.guidance_git import GitFault

        thread_id, paths, message = kwargs.get("_thread_id"), kwargs.get("paths"), kwargs.get("message")
        if not isinstance(thread_id, str) or not thread_id or not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths) or not isinstance(message, str) or not message.strip():
            return "Error: provide a current thread, paths, and a non-empty message."
        try:
            return await run_off_db(lambda: _commit(thread_id, paths, message))
        except (ValueError, OSError, GitFault) as exc:
            return f"Error: {exc}"


class WorkspaceHistoryTool(Tool):
    @property
    def name(self) -> str:
        return "workspace_history"

    @property
    def description(self) -> str:
        return "Read workspace path history; refuses fenced paths and their containing folders."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"path": {"type": "string"}, "count": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}}, "required": ["path"]}

    async def execute(self, **kwargs: Any) -> str:
        from infra.db_async import run_off_db
        from infra.guidance_git import GitFault

        thread_id, path, count = kwargs.get("_thread_id"), kwargs.get("path"), kwargs.get("count", 5)
        if not isinstance(thread_id, str) or not thread_id or not isinstance(path, str) or type(count) is not int or not 1 <= count <= 20:
            return "Error: provide a current thread, a path, and count from 1 to 20."
        try:
            return await run_off_db(lambda: _history(thread_id, path, count))
        except (ValueError, OSError, GitFault) as exc:
            return f"Error: {exc}"
