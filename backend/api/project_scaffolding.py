"""Project workspace scaffolding, source repos, and document roots."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from contextlib import nullcontext
from infra.time import utc_now_iso
from pathlib import Path

from loguru import logger

from core.workspace_scaffolding import (
    AtomicRenameUnavailableError,
    ScaffoldOwnership,
    atomic_rename_noreplace,
    remove_owned_scaffold,
    scaffold_project,
)
from api.source_entry_ownership import (
    SourceEntryChangedError,
    SourceEntryIdentity,
    capture_source_entry,
    publish_source_entry,
    remove_owned_source_entry,
    require_source_entry,
)
from infra import config
from infra.database import TransactionOwnershipError, write_transaction
from infra.project_repo_policy import (
    branch_pin_support_error,
    current_repo_branch,
    prepare_repo_policy,
    register_prepared_repo_policy,
)
from infra.repo_policy_transition import repo_policy_transition
from infra.workspace_paths import WorkspaceReference
from registry.bootload import report_sync_failure, sync_bootload_documents
from registry.shared_ownership_assignment import enforce_scaffold_ownership
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

PROJECT_CONTEXT_DEFAULT_FILES = [
    "context/PROJECT.md",
    "context/COORDINATION-MODEL.md",
]
BOOTLOADER_EXTS = (".md", ".txt")


def validate_bootloader_files(workspace_dir: Path, files: list) -> str | None:
    """Validate a project-relative bootloader file list."""
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, str) or not entry.strip():
            return "Entries must be non-empty paths"
        p = Path(entry)
        if p.is_absolute() or ".." in p.parts:
            return f"'{entry}' must be a relative path inside the project"
        if not entry.endswith(BOOTLOADER_EXTS):
            return f"'{entry}' — only {' / '.join(BOOTLOADER_EXTS)} files can be bootloaded"
        if entry in seen:
            return f"'{entry}' appears twice"
        seen.add(entry)
        if not (workspace_dir / entry).is_file():
            return f"'{entry}' not found in the project"
    return None


def _slugify(name: str) -> str:
    """Convert a project name to its workspace directory slug."""
    slug = name.lower().replace(" ", "-")
    if (
        not slug
        or slug in {".", ".."}
        or Path(slug).name != slug
        or "/" in slug
        or "\\" in slug
        or "\x00" in slug
    ):
        raise ValueError(
            "Project name must produce a single directory name inside projects/"
        )
    return slug


def _scaffold_project_workspace(
    name: str,
    description: str,
    *,
    with_ownership: bool = False,
) -> Path | tuple[Path, ScaffoldOwnership]:
    """Create ``<workspace>/projects/<slug>/`` with the canonical layout."""
    workspace_dir = config.WORKSPACE / "projects" / _slugify(name)
    return scaffold_project(
        workspace_dir,
        name=name,
        description=description,
        with_ownership=with_ownership,
    )


def validate_repo_name(name: str) -> str | None:
    """Return an error string if ``name`` is not a safe source/ entry name."""
    if not REPO_NAME_RE.match(name):
        return (
            "Repo name must start with a letter/digit and contain only "
            "letters, digits, dots, dashes, underscores"
        )
    return None


def _default_repo_name(target: Path) -> str | None:
    """Derive a visible source/ entry name from a target basename."""
    name = target.name.lstrip("._-")
    return name if name and not validate_repo_name(name) else None


def list_source_repos(workspace_dir: Path) -> list[dict]:
    """Scan ``<project>/source/`` and return the project's repos."""
    source_dir = workspace_dir / "source"
    if not source_dir.is_dir():
        return []

    repos: list[dict] = []
    for entry in sorted(source_dir.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            try:
                target = entry.resolve(strict=True)
                missing = not target.is_dir()
            except OSError:
                target = entry.readlink()
                if not target.is_absolute():
                    target = entry.parent / target
                target = target.resolve(strict=False)
                missing = True
            repos.append(_repo_row(entry.name, target, "linked", missing))
        elif entry.is_dir():
            repos.append(_repo_row(entry.name, entry.resolve(), "native", False))
    return repos


def _repo_row(name: str, path: Path, kind: str, missing: bool) -> dict:
    git_dir = path / ".git"
    is_repo = (not missing) and git_dir.exists()
    branch = None
    if is_repo:
        try:
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
            branch = (
                head.removeprefix("ref: refs/heads/")
                if head.startswith("ref:")
                else head[:8]
            )
        except OSError:
            branch = None
    return {
        "name": name,
        "path": str(path),
        "kind": kind,
        "missing": missing,
        "git": {"is_repo": is_repo, "branch": branch},
    }


def link_repo_into_source(
    workspace_dir: Path,
    target: Path,
    name: str | None = None,
    *,
    with_ownership: bool = False,
) -> dict | tuple[dict, SourceEntryIdentity]:
    """Symlink an existing directory into ``source/`` as a linked repo."""
    source_dir = workspace_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    def _within(child: Path, parent: Path) -> bool:
        return child == parent or parent in child.parents

    unresolved = Path(os.path.normpath(target.expanduser().absolute()))
    src_unresolved = Path(os.path.normpath(source_dir.absolute()))
    resolved = target.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"Not a directory: {resolved}")
    home = Path.home().resolve()
    if not _within(resolved, home):
        raise ValueError("Repo path must be under your home directory")
    if _within(unresolved, src_unresolved) or _within(resolved, source_dir.resolve()):
        raise ValueError(
            "Target is inside this project's source/ — already a native repo"
        )

    repo_name = name or _default_repo_name(resolved)
    if not repo_name:
        raise ValueError(
            f"Can't derive a repo name from '{resolved.name}' — pass one explicitly"
        )
    if err := validate_repo_name(repo_name):
        raise ValueError(err)
    link = source_dir / repo_name
    if link.exists() or link.is_symlink():
        raise ValueError(f"A repo named '{repo_name}' already exists in source/")

    staged = source_dir / f".{repo_name}.corsia-link-{uuid.uuid4().hex}"
    staged.symlink_to(resolved, target_is_directory=True)
    staged_identity = capture_source_entry(staged, "linked")
    identity = publish_source_entry(staged_identity, link)
    logger.info("Linked repo {} -> {}", link, resolved)
    row = _repo_row(repo_name, resolved, "linked", False)
    return (row, identity) if with_ownership else row


def create_repo_in_source(
    workspace_dir: Path,
    name: str,
    *,
    with_ownership: bool = False,
) -> dict | tuple[dict, SourceEntryIdentity]:
    """Create a native repo directory in ``source/`` and run ``git init``."""
    if err := validate_repo_name(name):
        raise ValueError(err)
    source_dir = workspace_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = source_dir / name
    if repo_dir.exists() or repo_dir.is_symlink():
        raise ValueError(f"A repo named '{name}' already exists in source/")

    staged = source_dir / f".{name}.corsia-native-{uuid.uuid4().hex}"
    staged.mkdir()
    staged_identity = capture_source_entry(staged, "native")
    try:
        proc = subprocess.run(
            ["git", "-c", "init.defaultRefFormat=files", "init", "-b", "main"],
            cwd=str(staged),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        remove_owned_source_entry(staged_identity)
        raise
    if proc.returncode != 0:
        remove_owned_source_entry(staged_identity)
        raise RuntimeError(
            f"git init failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    require_source_entry(staged_identity)
    identity = publish_source_entry(staged_identity, repo_dir)
    logger.info("Created native repo {}", repo_dir)
    row = _repo_row(name, repo_dir.resolve(), "native", False)
    return (row, identity) if with_ownership else row


def remove_linked_repo(
    workspace_dir: Path,
    name: str,
    *,
    expected_identity: SourceEntryIdentity | None = None,
) -> None:
    """Detach a linked repo symlink without touching the target."""
    entry = workspace_dir / "source" / name
    if not (entry.exists() or entry.is_symlink()):
        raise LookupError(f"No repo named '{name}'")
    if not entry.is_symlink():
        raise ValueError(
            "Native repos can't be detached from settings — move the folder out of source/"
        )
    identity = expected_identity or capture_source_entry(entry, "linked")
    if identity.entry != entry or identity.kind != "linked":
        raise SourceEntryChangedError(f"repository source binding '{name}' changed")
    remove_owned_source_entry(identity)
    logger.info("Detached linked repo {}", entry)


def compute_doc_roots(workspace_dir: Path) -> list[dict]:
    """Derive a project's document roots from its on-disk state."""
    roots: list[dict] = [
        {
            "path": str(workspace_dir),
            "purpose": "Project context and categorized shared planning documents.",
        }
    ]
    for repo in list_source_repos(workspace_dir):
        if repo["missing"]:
            continue
        roots.append(
            {"path": repo["path"], "purpose": f"Source repository ({repo['name']})."}
        )
    return roots


def _write_doc_roots(db, project_id: str, roots: list[dict]) -> None:
    """Update a project's ``doc_roots`` without committing."""
    db.execute(
        "UPDATE projects SET doc_roots = ? WHERE id = ?",
        (json.dumps(roots), project_id),
    )


def _active_owned_process_count() -> int:
    from host_access.processes import PROCESS_OWNER

    return PROCESS_OWNER.active_process_count()


def initialize_project(
    db,
    *,
    name: str,
    description: str = "",
    status: str = "active",
    repo_path_str: str | None = None,
    accent_color: str | None = None,
    user_id: str,
    conductor_model: str | None,
) -> dict:
    """Create a project row, workspace scaffold, repo link, and doc roots.

    ``user_id`` is the account the project's conductor thread belongs to; a
    project is created with its conductor, so there is no caller without one.
    ``conductor_model`` is that thread's selector; each caller states it
    (``infra.conductor.ensure_project_conductor``).
    """
    transition = (
        repo_policy_transition(active_process_count=_active_owned_process_count)
        if repo_path_str else nullcontext()
    )
    with transition:
        return _initialize_project(
            db,
            name=name,
            description=description,
            status=status,
            repo_path_str=repo_path_str,
            accent_color=accent_color,
            user_id=user_id,
            conductor_model=conductor_model,
        )


def _finalized_project_row(db, project_id: str, workspace_path: Path) -> dict:
    """Sync project bootload documents and return the created project row."""
    report_sync_failure(
        project_id,
        sync_bootload_documents(project_id, workspace_path, [*PROJECT_CONTEXT_DEFAULT_FILES], db=db),
        source="project_create",
    )
    row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row)


def _initialize_project(
    db,
    *,
    name: str,
    description: str,
    status: str,
    repo_path_str: str | None,
    accent_color: str | None,
    user_id: str,
    conductor_model: str | None,
) -> dict:
    """Perform project creation inside any required policy transition."""
    if db.in_transaction:
        raise TransactionOwnershipError("project creation requires a clean connection")
    project_id = str(uuid.uuid4())[:8]
    now = utc_now_iso()
    linked_identity: SourceEntryIdentity | None = None
    scaffold_ownership: ScaffoldOwnership | None = None
    policy = None

    try:
        workspace_path, scaffold_ownership = _scaffold_project_workspace(
            name, description, with_ownership=True,
        )
        if repo_path_str:
            linked_repo, linked_identity = link_repo_into_source(
                workspace_path,
                Path(repo_path_str),
                with_ownership=True,
            )
            require_source_entry(linked_identity)
            repo_path = Path(linked_repo["path"])
            if linked_repo["git"]["is_repo"]:
                if pin_error := branch_pin_support_error(repo_path):
                    raise ValueError(f"Repository cannot be pinned safely: {pin_error}")
                pinned_branch = current_repo_branch(repo_path)
                protect_base_checkout = True
            else:
                pinned_branch = None
                protect_base_checkout = False
            policy = prepare_repo_policy(
                project_id,
                linked_repo["name"],
                repo_path,
                pinned_branch=pinned_branch,
                protect_base_checkout=protect_base_checkout,
            )

        workspace_reference = WorkspaceReference.from_path(
            workspace_path, config.WORKSPACE_SELECTION,
        ).serialize()
        if linked_identity is not None:
            require_source_entry(linked_identity)
        doc_roots = compute_doc_roots(workspace_path)

        with write_transaction(db, scope="initialize_project"):
            db.execute(
                "INSERT INTO projects "
                "(id, name, path, description, status, accent_color, bootloader_files, "
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id, name, workspace_reference, description, status,
                    accent_color, json.dumps([]), now, now,
                ),
            )
            if policy is not None:
                register_prepared_repo_policy(policy, db=db)
            _write_doc_roots(db, project_id, doc_roots)
            from infra.conductor import ensure_project_conductor
            ensure_project_conductor(project_id, user_id, model=conductor_model, db=db)
            enforce_scaffold_ownership(config.WORKSPACE, project_id, db=db)
            if linked_identity is not None:
                require_source_entry(linked_identity)
    except Exception as exc:
        cleanup_errors: list[Exception] = []
        if linked_identity is not None:
            try:
                remove_owned_source_entry(linked_identity, missing_ok=True)
            except Exception as rollback_exc:
                cleanup_errors.append(rollback_exc)
        if scaffold_ownership is not None:
            try:
                remove_owned_scaffold(scaffold_ownership)
            except Exception as rollback_exc:
                cleanup_errors.append(rollback_exc)
        if cleanup_errors:
            details = "; ".join(str(error) for error in cleanup_errors)
            raise RuntimeError(
                f"{exc}; project initialization compensation failed: {details}"
            ) from exc
        raise

    return _finalized_project_row(db, project_id, workspace_path)
