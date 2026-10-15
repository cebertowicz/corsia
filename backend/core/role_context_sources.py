"""Role-scoped context selected from existing Agent, Conductor and CORSIA agent identity."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.effective_context import ContextSourceEntry
from core.thread_identity import thread_agent_identity
from infra.conductor import CoordinationScope, get_coordination_scopes

RoleName = Literal["agent", "conductor", "corsia_agent"]
RoleScope = Literal["workspace", "project"]

ROLE_ORDER: tuple[RoleName, ...] = ("agent", "conductor", "corsia_agent")


@dataclass(frozen=True)
class RoleScopeRoot:
    """One existing filesystem scope that may provide role context."""

    scope: RoleScope
    scope_id: str
    root: Path


@dataclass(frozen=True)
class RoleContextResolution:
    """Inferred roles, designation evidence, and their delivered context sources."""

    roles: tuple[RoleName, ...]
    conductor_scopes: tuple[CoordinationScope, ...]
    scope_roots: tuple[RoleScopeRoot, ...]
    sources: tuple[ContextSourceEntry, ...]

    def facts(self) -> dict:
        """Return snapshot-safe role facts for effective-context provenance."""
        return {
            "roles": list(self.roles),
            "receives_agent_role": "agent" in self.roles,
            "is_conductor": "conductor" in self.roles,
            "is_corsia_agent": "corsia_agent" in self.roles,
            "conductor_scopes": [dict(scope) for scope in self.conductor_scopes],
        }


def _conductor_scopes(
    thread_id: str,
    db,
    *,
    strict_db: bool,
) -> tuple[CoordinationScope, ...]:
    """Read current conductor designations, optionally requiring full schema."""
    if db is None:
        # DB-less reconstruction callers intentionally operate without live
        # identity state. They must remain ordinary rather than opening the
        # process-global DB or failing context reconstruction.
        return ()
    try:
        return tuple(get_coordination_scopes(thread_id, db=db))
    except sqlite3.OperationalError:
        if strict_db:
            raise
        # Standalone registry/unit consumers may intentionally use a minimal DB.
        return ()


def _thread_is_agent(thread_id: str, db, *, strict_db: bool) -> bool:
    """Project the canonical persistent-Agent flag when that schema is available."""
    if db is None:
        # Keep DB-less reconstruction symmetric with conductor inference: no
        # live identity evidence means no inferred role.
        return False
    try:
        row = db.execute(
            "SELECT * FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        if strict_db:
            raise
        return False
    if row is None:
        return False
    is_agent, _is_subagent, _title, _responsibility = thread_agent_identity(row)
    if is_agent is None and strict_db:
        raise sqlite3.OperationalError(
            "Agent identity unavailable: threads.agent_enabled missing"
        )
    return is_agent is True


def infer_thread_roles(
    thread_id: str,
    *,
    db,
    conductor_scopes: tuple[CoordinationScope, ...] | None = None,
    strict_db: bool = False,
) -> tuple[RoleName, ...]:
    """Infer the Agent, Conductor and CORSIA agent roles from existing state.

    The CORSIA agent is the workspace scope's occupant; a conductor is a
    project's. Each designation adds its own role on top of Agent, in
    ``ROLE_ORDER``.
    """
    scopes = (
        conductor_scopes
        if conductor_scopes is not None
        else _conductor_scopes(thread_id, db, strict_db=strict_db)
    )
    scope_types = {scope.get("scope_type") for scope in scopes}
    if scope_types:
        return tuple(
            role
            for role in ROLE_ORDER
            if role == "agent"
            or (role == "conductor" and "project" in scope_types)
            or (role == "corsia_agent" and "workspace" in scope_types)
        )
    is_agent = _thread_is_agent(thread_id, db, strict_db=strict_db)
    if is_agent:
        return ("agent",)
    return ()


def role_scope_roots(
    *,
    project_id: str | None,
    workspace: Path,
    db,
    strict_db: bool = False,
) -> tuple[RoleScopeRoot, ...]:
    """Return workspace then project roots without introducing a role scope."""
    from core.project_context import resolve_project_paths
    from core.workspace_layout import resolve_workspace_root

    roots = [
        RoleScopeRoot(
            scope="workspace",
            scope_id="workspace",
            root=resolve_workspace_root(workspace),
        )
    ]
    if not project_id:
        return tuple(roots)
    try:
        _name, planning_root = resolve_project_paths(
            workspace,
            project_id,
            db=db,
        )
    except sqlite3.OperationalError:
        if strict_db:
            raise
        return tuple(roots)
    if planning_root is not None:
        roots.append(
            RoleScopeRoot(
                scope="project",
                scope_id=project_id,
                root=planning_root.resolve(strict=False),
            )
        )
    return tuple(roots)


def role_directory(scope_root: Path, role: RoleName) -> Path:
    """Return a contained, non-symlink role directory for one existing scope."""
    root = scope_root.resolve(strict=False)
    context_root = root / "context"
    roles_root = context_root / "roles"
    role_root = roles_root / role
    for container in (context_root, roles_root, role_root):
        if container.is_symlink():
            raise RuntimeError(f"role context container cannot be a symlink: {container}")
    resolved = role_root.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise RuntimeError(f"role context escapes its scope: {role_root}")
    return role_root


def _role_markdown_files(scope_root: Path, role: RoleName) -> list[Path]:
    """Return sorted top-level Markdown files for one role and existing scope."""
    root = role_directory(scope_root, role)
    if not root.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(root.iterdir(), key=lambda candidate: candidate.name.casefold()):
        if path.suffix != ".md" or not path.is_file():
            continue
        if path.is_symlink():
            raise RuntimeError(f"role context file cannot be a symlink: {path}")
        files.append(path)
    return files


def _source_for_file(
    *,
    role: RoleName,
    scope_root: RoleScopeRoot,
    path: Path,
) -> ContextSourceEntry:
    """Read one role file into the canonical effective-context source record."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError(f"failed to read role context {path}: {error}") from error
    relative_path = path.relative_to(scope_root.root).as_posix()
    return ContextSourceEntry(
        source_id=(
            f"role-context:{scope_root.scope}:{scope_root.scope_id}:"
            f"{role}:{relative_path}"
        ),
        origin_section="role",
        label=f"{role.title()} role context: {relative_path}",
        stable_reference=str(path.resolve(strict=False)),
        freshness="fresh",
        inclusion="included",
        reason=f"{scope_root.scope} context selected for {role} role",
        owner_surface="document-viewer",
        prompt_content=f"## {relative_path}\n\n{content}",
        metadata={
            "provider": "role_context",
            "role": role,
            "scope": scope_root.scope,
            "scope_id": scope_root.scope_id,
            "relative_path": relative_path,
            "path": str(path.resolve(strict=False)),
            "repair_owner": "context",
        },
    )


def _error_source(
    *,
    thread_id: str,
    error: Exception,
    role: RoleName | None = None,
    scope_root: RoleScopeRoot | None = None,
    source_path: Path | None = None,
) -> ContextSourceEntry:
    """Expose unsafe or unavailable role selection without delivering fallback bytes."""
    scope = scope_root.scope if scope_root else "selection"
    scope_id = scope_root.scope_id if scope_root else thread_id
    role_label = role or "unknown"
    stable_path = (
        str(source_path.resolve(strict=False))
        if source_path is not None
        else (
            str(scope_root.root / "context" / "roles" / role)
            if scope_root and role
            else f"thread://{thread_id}/roles"
        )
    )
    source_suffix = f":{source_path.name}" if source_path is not None else ""
    relative_path = (
        source_path.relative_to(scope_root.root).as_posix()
        if source_path is not None and scope_root is not None
        else None
    )
    return ContextSourceEntry(
        source_id=f"role-context-error:{scope}:{scope_id}:{role_label}{source_suffix}",
        origin_section="role",
        label=f"Role context unavailable: {role_label}",
        stable_reference=stable_path,
        freshness="error",
        inclusion="omitted",
        reason=f"role context unavailable: {error}",
        owner_surface="document-viewer" if scope_root else "agents",
        metadata={
            "provider": "role_context",
            "role": role,
            "scope": scope,
            "scope_id": scope_id,
            "path": stable_path,
            "relative_path": relative_path,
            "repair_owner": "context" if scope_root else "agents",
            "errors": [str(error)],
        },
    )


def role_context_sources(
    roles: tuple[RoleName, ...],
    scope_roots: tuple[RoleScopeRoot, ...],
    *,
    thread_id: str,
) -> tuple[ContextSourceEntry, ...]:
    """Load role files in ``ROLE_ORDER``, outermost scope first."""
    sources: list[ContextSourceEntry] = []
    for role in roles:
        for scope_root in scope_roots:
            try:
                paths = _role_markdown_files(scope_root.root, role)
            except RuntimeError as error:
                sources.append(
                    _error_source(
                        thread_id=thread_id,
                        error=error,
                        role=role,
                        scope_root=scope_root,
                    )
                )
                continue
            for path in paths:
                try:
                    source = _source_for_file(
                        role=role,
                        scope_root=scope_root,
                        path=path,
                    )
                except RuntimeError as error:
                    sources.append(
                        _error_source(
                            thread_id=thread_id,
                            error=error,
                            role=role,
                            scope_root=scope_root,
                            source_path=path,
                        )
                    )
                    continue
                sources.append(source)
    return tuple(sources)


def resolve_role_context(
    *,
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    db,
    load_sources: bool = True,
    strict_db: bool = False,
) -> RoleContextResolution:
    """Resolve current roles and optionally load their filesystem context."""
    conductor_scopes = _conductor_scopes(thread_id, db, strict_db=strict_db)
    selection_errors: tuple[ContextSourceEntry, ...] = ()
    try:
        roles = infer_thread_roles(
            thread_id,
            db=db,
            conductor_scopes=conductor_scopes,
            strict_db=strict_db,
        )
    except sqlite3.OperationalError as error:
        roles = ()
        selection_errors = (
            _error_source(thread_id=thread_id, error=error),
        )
    try:
        scope_roots = role_scope_roots(
            project_id=project_id,
            workspace=workspace,
            db=db,
            strict_db=strict_db,
        )
    except sqlite3.OperationalError as error:
        scope_roots = role_scope_roots(
            project_id=None,
            workspace=workspace,
            db=db,
            strict_db=False,
        )
        selection_errors = (
            *selection_errors,
            _error_source(thread_id=thread_id, error=error),
        )
    sources = (
        (*selection_errors, *role_context_sources(roles, scope_roots, thread_id=thread_id))
        if load_sources
        else ()
    )
    return RoleContextResolution(
        roles=roles,
        conductor_scopes=conductor_scopes,
        scope_roots=scope_roots,
        sources=sources,
    )
