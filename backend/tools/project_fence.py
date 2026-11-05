"""The project roots one agent is fenced from, their Safehouse deny rules and refusal copy.

An agent's set is computed on every call and never cached, so a change to a
project's Local only switch or an agent's scope takes effect on its next tool call.
A local app's set is rendered at launch, so local apps relaunch when a project's
Local only switch changes; a repo registered later joins a running app's set at its
next launch, as that app could read the path before. The person's own app session is never fenced: only agent
identities reach this module. A project row, repo row or caller that can't be read
fences more, never less, and says so in a ``project_fence`` product event.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tools.host_path_resolution import canonical, contains

log = logging.getLogger(__name__)


def _event(action: str, thread_id: str | None = None, **properties) -> None:
    from infra.product_events import emit_product_event

    emit_product_event("project_fence", action, thread_id=thread_id, properties=properties)


def _failed_closed(reason: str, subject: object) -> None:
    log.warning("Project fence failed closed (%s): %s", reason, subject)
    _event("failed_closed", reason=reason)

_ORDINARY_COPY = (
    "`{path}` is in project `{name}`, outside your scope. "
    "Ask the CORSIA agent, or use `request_host_access` for reviewed temporary access."
)
_WORKSPACE_AGENT_COPY = (
    "`{path}` is in a workspace agent's private folder, outside your scope. "
    "Ask the CORSIA agent, or use `request_host_access` for reviewed temporary access."
)
_LOCAL_ONLY_COPY = (
    "`{path}` is in Local only project `{name}`, outside your scope. "
    "Host access doesn't open it. Ask its conductor{conductor}."
)


@dataclass(frozen=True, slots=True)
class FencedRoot:
    """One fenced folder as written, with what the refusal copy needs to name it."""

    path: Path
    project: str | None = None
    local_only: bool = False
    conductor: str | None = None

    @property
    def forms(self) -> tuple[Path, ...]:
        """The path as written and, only when it differs, resolved."""
        return tuple(dict.fromkeys((self.path, canonical(self.path))))


@dataclass(frozen=True, slots=True)
class Fence:
    """The fenced roots of one caller."""

    roots: tuple[FencedRoot, ...] = ()
    caller: str | None = None

    def _rules(self, local_only: bool, quote: Callable[[Path], str]) -> list[str]:
        return [
            f'(deny file-read* file-write* (subpath "{quote(form)}"))'
            for root in self.roots if root.local_only == local_only
            for form in root.forms
        ]

    def local_only_rules(self, quote: Callable[[Path], str]) -> list[str]:
        """Deny rules for Local only projects, the only roots a lease cannot open."""
        return self._rules(True, quote)

    def ordinary_rules(self, quote: Callable[[Path], str]) -> list[str]:
        """Deny rules for every other fenced root."""
        return self._rules(False, quote)

    def matching_root(self, path: Path) -> FencedRoot | None:
        """Return the strongest fenced root containing either form of a path."""
        if not self.roots:
            return None
        candidates = (Path(os.path.abspath(path.expanduser())), canonical(path))
        matches = [
            root for root in self.roots
            if any(contains(form, candidate) for form in root.forms for candidate in candidates)
        ]
        return next((root for root in matches if root.local_only), matches[0] if matches else None)

    def refusal(self, path: Path) -> str | None:
        """The approved refusal naming a fenced project path, or None."""
        root = self.matching_root(path)
        if root is None:
            return None
        _event("refused", self.caller, local_only=root.local_only)
        if root.project is None:
            return _WORKSPACE_AGENT_COPY.format(path=path)
        if not root.local_only:
            return _ORDINARY_COPY.format(path=path, name=root.project)
        who = f" (`{root.conductor}`)" if root.conductor else ""
        return _LOCAL_ONLY_COPY.format(path=path, name=root.project, conductor=who)


def _drop_nested(roots: list[FencedRoot]) -> tuple[FencedRoot, ...]:
    """Drop a root that sits inside another fenced root at least as strong.

    A root is dropped only when every form of it, written and resolved, is covered, because
    Seatbelt checks the resolved path. Only a Local only root covers a Local only one: a lease
    renders only those, so an ordinary root covering it would leave it open under the lease.
    """
    forms = [[str(form).casefold() for form in root.forms] for root in roots]

    def covered(index: int, root: FencedRoot, path: str) -> bool:
        return any(
            (path == form or path.startswith(form.rstrip("/") + "/"))
            and (candidate.local_only or not root.local_only)
            and (form != path or other < index or candidate.local_only != root.local_only)
            for other, candidate in enumerate(roots) if other != index
            for form in forms[other]
        )

    return tuple(
        root for index, root in enumerate(roots)
        if not all(covered(index, root, path) for path in forms[index])
    )


# A project is fenced from the caller when this says so: ``(project id, Local only)``.
_Fences = Callable[[str | None, bool], bool]


def _folder_roots(workspace: Path, db, fences: _Fences, rows, cloud_off) -> list[FencedRoot]:
    """Every folder on disk under ``projects/``, listed rather than registered, that the caller is fenced from.

    Fails closed: while a Local only project's folder does not resolve, any folder no
    project claims may be it, so each is fenced as Local only.
    """
    from core.project_context import resolve_project_row_paths

    by_root = {}
    lost_local_only = False
    for row in rows:
        try:
            _name, root = resolve_project_row_paths(workspace, project_name=row["name"], raw_path=row["path"])
        except (ValueError, OSError):
            root = None
        if root is not None:
            by_root[canonical(root)] = row
        elif row["id"] in cloud_off:
            _failed_closed("local_only_folder_unresolved", row["id"])
            lost_local_only = True
    projects_root = workspace / "projects"
    roots = []
    for child in sorted(projects_root.iterdir()) if projects_root.is_dir() else ():
        row = by_root.get(canonical(child))
        local_only = row["id"] in cloud_off if row is not None else lost_local_only
        if child.is_dir() and fences(row["id"] if row else None, local_only):
            roots.append(FencedRoot(
                child, row["name"] if row else child.name, local_only,
                row["conductor_thread_id"] if row else None,
            ))
    return roots


def _registered_paths(db) -> dict[str, list[Path]]:
    """Every registered repo folder by project, valid binding or not, so a bad row still fences."""
    by_project: dict[str, list[Path]] = {}
    for row in db.execute("SELECT project_id, repo_path FROM project_repo_policies"):
        if isinstance(path := row["repo_path"], str) and Path(path).is_absolute():
            by_project.setdefault(row["project_id"], []).append(Path(os.path.normpath(path)))
        else:
            _failed_closed("repo_path_unreadable", row["project_id"])
    return by_project


def _repo_roots(db, rows, fences: _Fences, cloud_off) -> list[FencedRoot]:
    """Fenced projects' registered repos, each as one whole root.

    A repo registered in two projects belongs to both: its worktrees' ``.git`` files
    point into one history, so folders can't split it. A root that a project the caller
    reaches holds a valid binding to stays open, unless a fenced Local only project also
    registers it and the caller reaches it only through projects that aren't: Local only
    content never reaches such a caller, so the whole repo is fenced, history included.
    Only a valid binding opens a root; any registered path fences one.
    """
    from infra.project_repo_policy import repo_policies_by_project

    valid = repo_policies_by_project(db=db)
    registered = _registered_paths(db)
    fenced = [row for row in rows if fences(row["id"], row["id"] in cloud_off)]
    fenced_ids = {row["id"] for row in fenced}
    # Each root the caller reaches, and whether a Local only project it reaches holds it.
    reached: dict[Path, bool] = {}
    for row in rows:
        if row["id"] not in fenced_ids:
            for policy in valid.get(row["id"], []):
                key = canonical(policy.path)
                reached[key] = reached.get(key, False) or row["id"] in cloud_off
    roots = []
    for row in fenced:
        local_only = row["id"] in cloud_off
        for path in registered.get(row["id"], []):
            key = canonical(path)
            if key not in reached or (local_only and not reached[key]):
                roots.append(FencedRoot(path, row["name"], local_only, row["conductor_thread_id"]))
    return roots


def _project_roots(workspace: Path, db, fences: _Fences) -> list[FencedRoot]:
    """The folders and the registered repos of every project the caller is fenced from."""
    from llm.locality import cloud_off_project_ids

    rows = db.execute("SELECT id, name, path, conductor_thread_id FROM projects").fetchall()
    cloud_off = cloud_off_project_ids(db=db)
    return _folder_roots(workspace, db, fences, rows, cloud_off) + _repo_roots(db, rows, fences, cloud_off)


def fence_for_thread(thread_id: str | None, *, db=None) -> Fence:
    """Compute the caller's fenced roots from the live workspace and database.

    No thread is the person's own session. A thread id with no row is fenced from
    every project, as a workspace agent is.
    """
    if not thread_id:
        return Fence()
    from infra.config import WORKSPACE
    from infra.corsia_agent import get_corsia_agent
    from infra.database import get_db

    db = db or get_db()
    workspace = Path(WORKSPACE)
    thread = db.execute("SELECT project_id, local_only FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if thread is None:
        _failed_closed("caller_unknown", thread_id)
        return Fence(_drop_nested(_project_roots(workspace, db, lambda _id, _local_only: True)), thread_id)
    corsia_agent = get_corsia_agent(db=db)
    if thread_id == corsia_agent:
        roots = [] if thread["local_only"] else _project_roots(workspace, db, lambda _id, local_only: local_only)
        return Fence(_drop_nested(roots), thread_id)
    own = thread["project_id"]
    roots = _project_roots(workspace, db, lambda project_id, _local_only: project_id != own or own is None)
    if own:
        agents = db.execute(
            "SELECT id FROM threads WHERE project_id IS NULL AND (agent_enabled = 1 OR id = ?) AND id != ?",
            (corsia_agent, thread_id),
        )
        roots.extend(FencedRoot(workspace / ".threads" / row["id"]) for row in agents)
    return Fence(_drop_nested(roots), thread_id)


def fence_for_local_mcp(*, db=None) -> Fence:
    """Every Local only project: a local MCP server has no caller, so no ordinary project is fenced."""
    from infra.config import WORKSPACE
    from infra.database import get_db

    roots = _project_roots(Path(WORKSPACE), db or get_db(), lambda _id, local_only: local_only)
    return Fence(_drop_nested(roots))
