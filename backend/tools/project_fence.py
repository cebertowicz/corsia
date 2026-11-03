"""The project roots one agent is fenced from, their Safehouse deny rules and refusal copy.

The set is computed for the calling thread on every call and never cached, so a
change to a project's Local only switch or an agent's scope takes effect on the
next tool call. The person's own app session is never fenced: only agent
identities reach this module.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tools.host_path_resolution import canonical, contains

log = logging.getLogger(__name__)

_ORDINARY_COPY = (
    "`{path}` is in project `{name}`, outside your scope. "
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

    def refusal(self, path: Path) -> str | None:
        """The approved refusal naming a fenced project path, or None."""
        candidate = canonical(path)
        matches = [
            root for root in self.roots
            if root.project is not None and any(contains(form, candidate) for form in root.forms)
        ]
        # A Local only root inside an ordinary one still holds under a lease, so its copy wins.
        root = next((root for root in matches if root.local_only), matches[0] if matches else None)
        if root is None:
            return None
        if not root.local_only:
            return _ORDINARY_COPY.format(path=path, name=root.project)
        who = f" (`{root.conductor}`)" if root.conductor else ""
        return _LOCAL_ONLY_COPY.format(path=path, name=root.project, conductor=who)


def _drop_nested(roots: list[FencedRoot]) -> tuple[FencedRoot, ...]:
    """Drop a root that sits inside another fenced root at least as strong.

    Only a Local only root covers a Local only one: a lease renders only those, so an
    ordinary root covering it would leave it open under the lease.
    """
    # Resolve each root once; the pairwise pass compares text, as ``contains`` does.
    paths = [str(root.path).casefold() for root in roots]
    forms = [[str(form).casefold() for form in root.forms] for root in roots]
    kept = []
    for index, root in enumerate(roots):
        path = paths[index]
        inside = any(
            (path == form or path.startswith(form.rstrip("/") + "/"))
            and (candidate.local_only or not root.local_only)
            and (form != path or other < index or candidate.local_only != root.local_only)
            for other, candidate in enumerate(roots) if other != index
            for form in forms[other]
        )
        if not inside:
            kept.append(root)
    return tuple(kept)


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
            log.warning("Local only project %s has no resolvable folder; fencing unclaimed folders as Local only", row["id"])
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


def _attributed_worktrees(db, repo: Path) -> list[tuple[Path, str | None]]:
    """Each verified worktree of ``repo`` with the project of the thread that made it.

    ``worktree_create`` names a worktree ``<thread id>-<descriptor>``, so the thread
    is whichever hyphen-delimited prefix of the name is a thread id.
    """
    from tools.sandbox_config import list_repo_worktrees

    worktrees = list_repo_worktrees(repo)
    prefixes = {
        tree: [tree.name[:i] for i, char in enumerate(tree.name) if char == "-"] or [tree.name]
        for tree in worktrees
    }
    wanted = sorted({prefix for options in prefixes.values() for prefix in options})
    marks = ",".join("?" * len(wanted))
    owners = {
        row["id"]: row["project_id"]
        for row in db.execute(f"SELECT id, project_id FROM threads WHERE id IN ({marks})", wanted)
    } if wanted else {}
    return [
        (tree, next((owners[p] for p in options if p in owners), None))
        for tree, options in prefixes.items()
    ]


def _repo_roots(db, rows, fences: _Fences, cloud_off) -> list[FencedRoot]:
    """Fenced projects' registered repos: the canonical root, or only their own worktrees when shared.

    A linked worktree's ``.git`` points into ``<canonical>/.git/worktrees/``, so a root a
    project the caller may reach also registers is never fenced.
    """
    from infra.project_repo_policy import repo_policies_by_project

    by_project = repo_policies_by_project(db=db)
    fenced: dict = {}
    shared: set[Path] = set()
    for row in rows:
        policies = by_project.get(row["id"], [])
        if fences(row["id"], row["id"] in cloud_off):
            fenced[row["id"]] = (row, policies)
        else:
            shared.update(canonical(policy.path) for policy in policies)
    roots = []
    attributed: dict[Path, list[tuple[Path, str | None]]] = {}
    for project_id, (row, policies) in fenced.items():
        local_only = project_id in cloud_off
        for policy in policies:
            if (key := canonical(policy.path)) not in shared:
                roots.append(FencedRoot(policy.path, row["name"], local_only, row["conductor_thread_id"]))
                continue
            if key not in attributed:
                attributed[key] = _attributed_worktrees(db, policy.path)
            roots.extend(
                FencedRoot(tree, row["name"], local_only, row["conductor_thread_id"])
                for tree, owner in attributed[key] if owner == project_id
            )
    return roots


def _project_roots(workspace: Path, db, fences: _Fences) -> list[FencedRoot]:
    """The folders and the registered repos of every project the caller is fenced from."""
    from llm.locality import cloud_off_project_ids

    rows = db.execute("SELECT id, name, path, conductor_thread_id FROM projects").fetchall()
    cloud_off = cloud_off_project_ids(db=db)
    return _folder_roots(workspace, db, fences, rows, cloud_off) + _repo_roots(db, rows, fences, cloud_off)


def fence_for_thread(thread_id: str | None, *, db=None) -> Fence:
    """Compute the caller's fenced roots from the live workspace and database."""
    if not thread_id:
        return Fence()
    from infra.config import WORKSPACE
    from infra.corsia_agent import get_corsia_agent
    from infra.database import get_db

    db = db or get_db()
    workspace = Path(WORKSPACE)
    thread = db.execute("SELECT project_id, local_only FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if thread is None:
        return Fence()
    corsia_agent = get_corsia_agent(db=db)
    if thread_id == corsia_agent:
        roots = [] if thread["local_only"] else _project_roots(workspace, db, lambda _id, local_only: local_only)
        return Fence(_drop_nested(roots))
    own = thread["project_id"]
    roots = _project_roots(workspace, db, lambda project_id, _local_only: project_id != own or own is None)
    if own:
        agents = db.execute(
            "SELECT id FROM threads WHERE project_id IS NULL AND (agent_enabled = 1 OR id = ?) AND id != ?",
            (corsia_agent, thread_id),
        )
        roots.extend(FencedRoot(workspace / ".threads" / row["id"]) for row in agents)
    return Fence(_drop_nested(roots))


def fence_for_local_mcp(*, db=None) -> Fence:
    """Every Local only project: a local MCP server has no caller, so no ordinary project is fenced."""
    from infra.config import WORKSPACE
    from infra.database import get_db

    roots = _project_roots(Path(WORKSPACE), db or get_db(), lambda _id, local_only: local_only)
    return Fence(_drop_nested(roots))
