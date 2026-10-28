"""Manage visible source bindings and their authoritative repository policies."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
import threading
from typing import Callable, TypeVar

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from core.project_context import resolve_project_paths
from infra.auth import authenticate
from infra import config
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.project_repo_policy import (
    RepoPolicy,
    RepoGitTimeout,
    branch_pin_support_error,
    clear_repo_policy,
    current_repo_branch,
    get_project_repo_policies,
    list_repo_branches,
    register_repo_policy,
    restore_repo_branch_switch,
    switch_repo_branch,
)
from infra.repo_policy_transition import (
    RepoPolicyTransitionBusy,
    repo_policy_transition,
)

from api.project_repo_checkout_guards import (
    capture_clean_checkout,
    repo_git_timeout_response,
    require_checkout_unchanged,
    require_supported_pin,
)
from api.project_scaffolding import (
    SourceEntryChangedError,
    SourceEntryIdentity,
    _write_doc_roots,
    capture_source_entry,
    compute_doc_roots,
    create_repo_in_source,
    link_repo_into_source,
    list_source_repos,
    remove_owned_source_entry,
    remove_linked_repo,
    require_source_entry,
)

router = APIRouter(prefix="/api/projects", tags=["project-repos"])

_T = TypeVar("_T")
_PROJECT_REPO_LOCKS_GUARD = threading.Lock()
_PROJECT_REPO_LOCKS: dict[str, threading.RLock] = {}


class RepoAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str  # "link" | "create"
    path: str | None = None  # link mode: the existing directory to attach
    name: str | None = None  # optional for link (defaults from basename); required for create
    pinned_branch: str | None = None
    protect_base_checkout: bool | None = None


class RepoUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned_branch: str | None = None
    protect_base_checkout: bool | None = None


def _workspace_dir_for(project_id: str) -> Path:
    """Resolve the project's planning dir or raise 404."""
    db = get_db()
    try:
        project_name, ws_dir = resolve_project_paths(
            config.WORKSPACE,
            project_id,
            db=db,
        )
    finally:
        db.close()
    if project_name is None:
        raise HTTPException(404, "Project not found")
    if ws_dir is None or not ws_dir.is_dir():
        raise HTTPException(409, f"Project workspace dir missing: {ws_dir}")
    return ws_dir


def _sync(project_id: str, ws_dir: Path, db) -> None:
    """Update doc roots inside the caller's compensation transaction."""
    from events.connectors.github_screen import install
    _write_doc_roots(db, project_id, compute_doc_roots(ws_dir))
    install.sync(db, project_id)


def _under_project_repo_lock(project_id: str, operation: Callable[[], _T]) -> _T:
    """Serialize one project's filesystem, Git, and row compensation unit."""
    with _PROJECT_REPO_LOCKS_GUARD:
        lock = _PROJECT_REPO_LOCKS.setdefault(project_id, threading.RLock())
    with lock:
        return operation()


def _with_policy(repo: dict, policies: dict[str, RepoPolicy]) -> dict:
    """Attach policy state only when the visible binding still matches its row."""
    row = dict(repo)
    policy = policies.get(repo["name"])
    bound = (
        policy is not None
        and not repo.get("missing", False)
        and policy.path
        == Path(str(repo["path"])).expanduser().resolve(strict=False)
    )
    row["registered"] = bound
    row["pinned_branch"] = policy.pinned_branch if bound else None
    row["protect_base_checkout"] = policy.protect_base_checkout if bound else False
    # Current clients still render this derived response field. Mutations use
    # only the two canonical controls above.
    row["branch_pinned"] = bool(bound and policy.branch_pinned)
    return row


def _remove_created_source(
    identity: SourceEntryIdentity,
    *,
    tolerate_replacement: bool = False,
) -> None:
    """Remove only the exact source object created by this mutation."""
    try:
        remove_owned_source_entry(identity, missing_ok=True)
    except SourceEntryChangedError:
        if tolerate_replacement:
            return
        raise


def _require_owned_source(identity: SourceEntryIdentity) -> None:
    try:
        require_source_entry(identity)
    except SourceEntryChangedError as exc:
        raise HTTPException(409, str(exc)) from exc


def _restore_detached_link(
    entry: Path,
    raw_target: Path,
    *,
    tolerate_replacement: bool = False,
) -> None:
    if entry.exists() or entry.is_symlink():
        if tolerate_replacement:
            return
        raise RuntimeError("detached repository source path was replaced")
    entry.symlink_to(raw_target, target_is_directory=True)


def _run_compensation(
    original: Exception,
    actions: list[tuple[str, Callable[[], None]]],
) -> None:
    failures: list[str] = []
    for label, action in actions:
        try:
            action()
        except Exception as exc:
            failures.append(f"{label}: {exc}")
    if failures:
        raise RuntimeError(
            f"{original}; rollback also failed ({'; '.join(failures)})"
        ) from original


def _require_source_binding(
    ws_dir: Path,
    name: str,
    expected_path: Path,
) -> dict:
    repo = next(
        (row for row in list_source_repos(ws_dir) if row["name"] == name),
        None,
    )
    if repo is None or repo.get("missing", False):
        raise HTTPException(
            409,
            f"Repository source binding '{name}' disappeared during the update",
        )
    current_path = Path(str(repo["path"])).expanduser().resolve(strict=False)
    if current_path != expected_path:
        raise HTTPException(
            409,
            f"Repository source binding '{name}' changed during the update",
        )
    return repo


def _require_policy_snapshot(
    project_id: str,
    name: str,
    expected: RepoPolicy | None,
    *,
    db,
) -> None:
    current = get_project_repo_policies(project_id, db=db).get(name)
    raw_exists = db.execute(
        "SELECT 1 FROM project_repo_policies "
        "WHERE project_id = ? AND repo_name = ?",
        (project_id, name),
    ).fetchone() is not None
    if current != expected or raw_exists != (expected is not None):
        raise HTTPException(
            409,
            f"Repository policy '{name}' changed during the update",
        )


def _validated_repo_and_policy(
    project_id: str,
    ws_dir: Path,
    name: str,
    *,
    db,
) -> tuple[dict, RepoPolicy]:
    repo = next(
        (row for row in list_source_repos(ws_dir) if row["name"] == name),
        None,
    )
    if repo is None:
        raise HTTPException(404, f"No repo named '{name}'")
    if repo.get("missing", False):
        raise HTTPException(409, f"Repository source entry '{name}' is missing")
    policy = get_project_repo_policies(project_id, db=db).get(name)
    repo_path = Path(str(repo["path"])).expanduser().resolve(strict=False)
    if policy is None or policy.path != repo_path:
        raise HTTPException(
            409,
            "Repository is not registered or its source binding has changed",
        )
    return repo, policy


def _add_controls(
    body: RepoAdd,
    *,
    is_git: bool,
    current_branch: str | None,
) -> tuple[str | None, bool]:
    fields = body.model_fields_set
    if (
        "protect_base_checkout" in fields
        and body.protect_base_checkout is None
    ):
        raise HTTPException(400, "protect_base_checkout must be true or false")
    if not is_git:
        if (
            ("pinned_branch" in fields and body.pinned_branch is not None)
            or (
                "protect_base_checkout" in fields
                and body.protect_base_checkout
            )
        ):
            raise HTTPException(
                409,
                "Branch pinning and checkout protection require a Git repository",
            )
        return None, False
    pinned = (
        body.pinned_branch
        if "pinned_branch" in fields
        else current_branch
    )
    protected = (
        bool(body.protect_base_checkout)
        if "protect_base_checkout" in fields
        else True
    )
    return pinned, protected


def _update_controls(
    body: RepoUpdate,
    *,
    policy: RepoPolicy,
) -> tuple[str | None, bool]:
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(400, "No repository settings to update")
    if (
        "protect_base_checkout" in fields
        and body.protect_base_checkout is None
    ):
        raise HTTPException(400, "protect_base_checkout must be true or false")
    return (
        (
            body.pinned_branch
            if "pinned_branch" in fields
            else policy.pinned_branch
        ),
        (
            bool(body.protect_base_checkout)
            if "protect_base_checkout" in fields
            else policy.protect_base_checkout
        ),
    )


def _active_owned_process_count() -> int:
    from host_access.processes import PROCESS_OWNER

    return PROCESS_OWNER.active_process_count()


def _commit_repo_policy_update(
    *,
    project_id: str,
    name: str,
    ws_dir: Path,
    repo_path: Path,
    existing: RepoPolicy,
    pinned_branch: str | None,
    protected: bool,
    selects_pin: bool,
    verify_checkout: bool,
    connection,
) -> dict:
    switched = None
    try:
        if selects_pin:
            require_supported_pin(repo_path, branch_pin_support_error)
            try:
                switched = switch_repo_branch(repo_path, pinned_branch)
            except RepoGitTimeout as exc:
                raise repo_git_timeout_response(exc) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        expected_checkout = (
            capture_clean_checkout(repo_path)
            if verify_checkout
            else None
        )
        with write_transaction(connection, scope="repo_policy_update"):
            _require_policy_snapshot(project_id, name, existing, db=connection)
            _require_source_binding(ws_dir, name, repo_path)
            policy = register_repo_policy(
                project_id,
                name,
                repo_path,
                pinned_branch=pinned_branch,
                protect_base_checkout=protected,
                db=connection,
            )
            current_row = _require_source_binding(ws_dir, name, repo_path)
            response = _with_policy(current_row, {name: policy})
            _require_policy_snapshot(project_id, name, policy, db=connection)
            _require_source_binding(ws_dir, name, repo_path)
            if expected_checkout is not None:
                require_checkout_unchanged(repo_path, expected_checkout)
        return response
    except Exception as exc:
        actions: list[tuple[str, Callable[[], None]]] = [
            ("database", connection.rollback)
        ]
        if switched is not None:
            actions.append(
                (
                    "branch",
                    lambda: restore_repo_branch_switch(repo_path, switched),
                )
            )
        _run_compensation(exc, actions)
        raise


@router.get("/{project_id}/repos")
async def list_repos(token: str, project_id: str):
    """List the project's repos."""
    if not authenticate(token):
        raise HTTPException(401)

    def _list():
        ws_dir = _workspace_dir_for(project_id)
        policies = get_project_repo_policies(project_id)
        return [
            _with_policy(repo, policies)
            for repo in list_source_repos(ws_dir)
        ]

    return {"repos": await run_in_db(_list)}


def _resolve_add_source(
    ws_dir: Path,
    body: RepoAdd,
) -> tuple[dict, SourceEntryIdentity]:
    """Resolve the source binding an add creates, before its transaction opens."""
    if body.mode == "link":
        if not body.path:
            raise HTTPException(400, "link mode requires 'path'")
        try:
            row, identity = link_repo_into_source(
                ws_dir,
                Path(body.path),
                name=body.name,
                with_ownership=True,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(400, f"Bad repository path: {exc}") from exc
    else:
        if not body.name:
            raise HTTPException(400, "create mode requires 'name'")
        try:
            row, identity = create_repo_in_source(
                ws_dir,
                body.name,
                with_ownership=True,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
    return row, identity


def _commit_add_policy(
    project_id: str,
    ws_dir: Path,
    row: dict,
    identity: SourceEntryIdentity,
    repo_path: Path,
    pinned_branch: str | None,
    protected: bool,
    connection,
) -> dict:
    """Register one add's policy row inside the caller's compensation transaction."""
    name = row["name"]
    with write_transaction(connection, scope="repo_policy_add"):
        _require_policy_snapshot(project_id, name, None, db=connection)
        _require_source_binding(ws_dir, name, repo_path)
        _require_owned_source(identity)
        policy = register_repo_policy(
            project_id,
            name,
            repo_path,
            pinned_branch=pinned_branch,
            protect_base_checkout=protected,
            db=connection,
        )
        _sync(project_id, ws_dir, connection)
        current_row = _require_source_binding(ws_dir, name, repo_path)
        response = _with_policy(current_row, {name: policy})
        _require_policy_snapshot(project_id, name, policy, db=connection)
        _require_source_binding(ws_dir, name, repo_path)
        _require_owned_source(identity)
    return response


def _select_add_policy(row: dict, body: RepoAdd, repo_path: Path):
    """Resolve add controls and any controlled branch switch before persistence."""
    needs_current_branch = bool(row["git"]["is_repo"]) and (
        "pinned_branch" not in body.model_fields_set
        or body.pinned_branch is not None
    )
    try:
        current = current_repo_branch(repo_path) if needs_current_branch else None
    except RepoGitTimeout as exc:
        raise repo_git_timeout_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    pinned_branch, protected = _add_controls(
        body,
        is_git=bool(row["git"]["is_repo"]),
        current_branch=current,
    )
    if pinned_branch is None:
        return pinned_branch, protected, None
    require_supported_pin(repo_path, branch_pin_support_error)
    try:
        switched = switch_repo_branch(repo_path, pinned_branch)
    except RepoGitTimeout as exc:
        raise repo_git_timeout_response(exc) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return pinned_branch, protected, switched


def register_source_repo(project_id: str, body: RepoAdd) -> dict:
    """Add-repo procedure shared by Project settings and project_repo_register."""

    def _add():
        ws_dir = _workspace_dir_for(project_id)
        connection = None
        identity: SourceEntryIdentity | None = None
        repo_path: Path | None = None
        switched = None
        try:
            row, identity = _resolve_add_source(ws_dir, body)
            _require_owned_source(identity)
            repo_path = Path(str(row["path"])).expanduser().resolve(strict=False)
            pinned_branch, protected, switched = _select_add_policy(
                row,
                body,
                repo_path,
            )
            connection = get_db()
            return _commit_add_policy(
                project_id,
                ws_dir,
                row,
                identity,
                repo_path,
                pinned_branch,
                protected,
                connection,
            )
        except Exception as exc:
            concurrent_conflict = (
                isinstance(exc, HTTPException) and exc.status_code == 409
            )
            actions: list[tuple[str, Callable[[], None]]] = []
            if connection is not None:
                actions.append(("database", connection.rollback))
            if switched is not None and repo_path is not None:
                actions.append(
                    (
                        "branch",
                        lambda: restore_repo_branch_switch(repo_path, switched),
                    )
                )
            if identity is not None:
                actions.append(
                    (
                        "source binding",
                        lambda: _remove_created_source(
                            identity,
                            tolerate_replacement=concurrent_conflict,
                        ),
                    )
                )
            _run_compensation(exc, actions)
            raise
        finally:
            if connection is not None:
                connection.close()

    def _transitioned_add():
        try:
            with repo_policy_transition(active_process_count=_active_owned_process_count):
                return _add()
        except RepoPolicyTransitionBusy as exc:
            raise HTTPException(409, str(exc)) from exc

    return _under_project_repo_lock(project_id, _transitioned_add)


@router.post("/{project_id}/repos")
async def add_repo(token: str, project_id: str, body: RepoAdd):
    """Attach an existing directory (mode=link) or create a new repo (mode=create)."""
    if not authenticate(token):
        raise HTTPException(401)
    if body.mode not in ("link", "create"):
        raise HTTPException(400, "mode must be 'link' or 'create'")

    repo = await run_in_db(lambda: register_source_repo(project_id, body))
    return {"added": True, "repo": repo}


@router.patch("/{project_id}/repos/{name}")
async def update_repo(token: str, project_id: str, name: str, body: RepoUpdate):
    """Update branch pinning and base-checkout protection independently."""
    if not authenticate(token):
        raise HTTPException(401)

    def _update():
        ws_dir = _workspace_dir_for(project_id)
        connection = get_db()
        try:
            repo, existing = _validated_repo_and_policy(
                project_id,
                ws_dir,
                name,
                db=connection,
            )
            repo_path = Path(str(repo["path"])).expanduser().resolve(strict=False)
            pinned_branch, protected = _update_controls(
                body,
                policy=existing,
            )
            if not repo["git"]["is_repo"] and (
                pinned_branch is not None or protected
            ):
                raise HTTPException(
                    409,
                    "Branch pinning and checkout protection require a Git repository",
                )
            selects_pin = (
                "pinned_branch" in body.model_fields_set
                and pinned_branch is not None
            )
            changes_policy = (
                pinned_branch != existing.pinned_branch
                or protected != existing.protect_base_checkout
            )
            verify_checkout = changes_policy or selects_pin
            transition = (
                repo_policy_transition(
                    active_process_count=_active_owned_process_count,
                )
                if verify_checkout
                else nullcontext()
            )
            try:
                with transition:
                    return _commit_repo_policy_update(
                        project_id=project_id,
                        name=name,
                        ws_dir=ws_dir,
                        repo_path=repo_path,
                        existing=existing,
                        pinned_branch=pinned_branch,
                        protected=protected,
                        selects_pin=selects_pin,
                        verify_checkout=verify_checkout,
                        connection=connection,
                    )
            except RepoPolicyTransitionBusy as exc:
                raise HTTPException(409, str(exc)) from exc
        finally:
            connection.close()

    return {
        "updated": True,
        "repo": await run_in_db(
            lambda: _under_project_repo_lock(project_id, _update)
        ),
    }


@router.get("/{project_id}/repos/{name}/branches")
async def list_branches(token: str, project_id: str, name: str):
    """List local and uniquely-owned cached remote branches."""
    if not authenticate(token):
        raise HTTPException(401)

    def _list():
        ws_dir = _workspace_dir_for(project_id)
        connection = get_db()
        try:
            repo, _policy = _validated_repo_and_policy(
                project_id,
                ws_dir,
                name,
                db=connection,
            )
            if not repo["git"]["is_repo"]:
                raise HTTPException(409, "Branch listing requires a Git repository")
            repo_path = Path(str(repo["path"])).expanduser().resolve(strict=False)
            return {
                "current_branch": current_repo_branch(repo_path),
                "branches": [
                    asdict(branch)
                    for branch in list_repo_branches(repo_path)
                ],
            }
        except RepoGitTimeout as exc:
            raise repo_git_timeout_response(exc) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            connection.close()

    return await run_in_db(
        lambda: _under_project_repo_lock(project_id, _list)
    )


def _commit_detach_policy(
    project_id: str,
    name: str,
    ws_dir: Path,
    entry: Path,
    existing: RepoPolicy,
    connection,
) -> None:
    """Clear one detach's policy row inside the caller's compensation transaction."""
    with write_transaction(connection, scope="repo_policy_detach"):
        _require_policy_snapshot(project_id, name, existing, db=connection)
        clear_repo_policy(project_id, name, db=connection)
        _sync(project_id, ws_dir, connection)
        _require_policy_snapshot(project_id, name, None, db=connection)
        if entry.exists() or entry.is_symlink():
            raise HTTPException(
                409,
                f"Repository source binding '{name}' reappeared during detach",
            )


@router.delete("/{project_id}/repos/{name}")
async def detach_repo(token: str, project_id: str, name: str):
    """Detach a LINKED repo (symlink removed; target untouched). Native → 409."""
    if not authenticate(token):
        raise HTTPException(401)

    def _detach():
        ws_dir = _workspace_dir_for(project_id)
        entry = ws_dir / "source" / name
        raw_target: Path | None = None
        removed = False
        connection = get_db()
        try:
            if not (entry.exists() or entry.is_symlink()):
                raise HTTPException(404, f"No repo named '{name}'")
            if not entry.is_symlink():
                raise HTTPException(
                    409,
                    "Native repos can't be detached from settings",
                )
            try:
                identity = capture_source_entry(entry, "linked")
            except OSError as exc:
                raise HTTPException(
                    409,
                    f"Repository source binding '{name}' changed",
                ) from exc
            _repo, existing = _validated_repo_and_policy(
                project_id,
                ws_dir,
                name,
                db=connection,
            )
            _require_owned_source(identity)
            raw_target = identity.raw_target
            try:
                remove_linked_repo(
                    ws_dir,
                    name,
                    expected_identity=identity,
                )
                removed = True
            except LookupError as exc:
                raw_target = None
                raise HTTPException(404, str(exc)) from exc
            except SourceEntryChangedError as exc:
                raw_target = None
                raise HTTPException(409, str(exc)) from exc
            except ValueError as exc:
                raw_target = None
                raise HTTPException(409, str(exc)) from exc
            _commit_detach_policy(project_id, name, ws_dir, entry, existing, connection)
        except Exception as exc:
            concurrent_conflict = (
                isinstance(exc, HTTPException) and exc.status_code == 409
            )
            actions: list[tuple[str, Callable[[], None]]] = [
                ("database", connection.rollback)
            ]
            if removed and raw_target is not None:
                actions.append(
                    (
                        "source binding",
                        lambda: _restore_detached_link(
                            entry,
                            raw_target,
                            tolerate_replacement=concurrent_conflict,
                        ),
                    )
                )
            _run_compensation(exc, actions)
            raise
        finally:
            connection.close()

    def _transitioned_detach():
        try:
            with repo_policy_transition(
                active_process_count=_active_owned_process_count,
            ):
                return _detach()
        except RepoPolicyTransitionBusy as exc:
            raise HTTPException(409, str(exc)) from exc

    await run_in_db(
        lambda: _under_project_repo_lock(project_id, _transitioned_detach)
    )
    return {"detached": True, "name": name}
