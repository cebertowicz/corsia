"""Canonical policy for durable writes under a scope's ``shared/`` tree."""

from __future__ import annotations

from pathlib import Path
import re

from core.workspace_layout import resolve_project_root
from infra.conductor import WORKSPACE_SCOPE
from infra.database import get_db
from infra.project_repo_policy import list_registered_repo_policies
from registry.document_ownership import (
    document_write_authority,
    gate_existing_document,
    recover_moved_identity,
)
from registry.shared_document_messages import (
    missing_category_message,
    structure_owned_message,
)
from registry.shared_principals import (
    UI_PRINCIPAL,
    acting_owner,
    resolve_explicit_owner,
    scope_conductor,
)
from registry.shared_target import SharedTarget
from registry.shared_write_errors import (
    DocumentOwnershipRefused,
    DurableWritePolicyError,
    ThreadUnavailableError,
)
from tools.sandbox_config import registered_repo_for_path

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def _scope_root(
    *,
    workspace: Path,
    thread_id: str,
    project_id: str | None,
    direct_user: bool,
    db,
) -> tuple[Path, str | None]:
    if direct_user:
        row = db.execute(
            "SELECT project_id FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        authoritative_project = project_id if row is None else row["project_id"]
        if row is not None and project_id is not None and authoritative_project != project_id:
            raise DurableWritePolicyError("The write scope no longer matches the current thread.")
    else:
        row = db.execute(
            "SELECT project_id FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise ThreadUnavailableError("The current CORSIA thread is unavailable.")
        authoritative_project = row["project_id"]
        if authoritative_project != project_id:
            raise DurableWritePolicyError("The write scope no longer matches the current thread.")
    if authoritative_project is None:
        return workspace.resolve(strict=False), None
    root = resolve_project_root(workspace, authoritative_project, db=db)
    if root is None:
        raise DurableWritePolicyError("The current project scope is unavailable.")
    return root.resolve(strict=False), str(authoritative_project)


def _approved_categories(shared_root: Path) -> tuple[str, ...]:
    if not shared_root.is_dir() or shared_root.is_symlink():
        return ()
    categories = [
        child.name
        for child in shared_root.iterdir()
        if child.is_dir()
        and not child.is_symlink()
        and (child / "INDEX.md").is_file()
        and not (child / "INDEX.md").is_symlink()
    ]
    return tuple(sorted(categories, key=str.casefold))


def _dated_trigger(parts: tuple[str, ...]) -> str | None:
    return "none: dated record" if any(_DATE_PREFIX.match(part) for part in parts) else None


def _creation_owner(
    *,
    thread_id: str,
    project_id: str | None,
    direct_user: bool,
    requested_owner: str | None,
    db,
) -> str:
    persistent = acting_owner(thread_id, False, db=db)
    if direct_user:
        return persistent or UI_PRINCIPAL
    if persistent:
        return persistent
    if not requested_owner or not requested_owner.strip():
        raise DurableWritePolicyError(
            "Creating a shared file without a persistent caller requires `owner`: "
            "an agent name or ID, `conductor`, or `user`."
        )
    caller = db.execute(
        "SELECT user_id FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    if caller is None:
        raise ThreadUnavailableError("The current CORSIA thread is unavailable.")
    try:
        return resolve_explicit_owner(
            requested_owner,
            project_id=project_id,
            user_id=str(caller["user_id"]),
            db=db,
        )
    except ValueError as exc:
        raise DurableWritePolicyError(str(exc)) from exc


def _shared_candidate(
    target: Path,
    *,
    workspace: Path,
    thread_id: str,
    project_id: str | None,
    direct_user: bool,
    db,
) -> tuple[Path, Path, str | None, str | None, tuple[str, ...]] | None:
    scope_root, authoritative_project = _scope_root(
        workspace=workspace,
        thread_id=thread_id,
        project_id=project_id,
        direct_user=direct_user,
        db=db,
    )
    candidate = target.expanduser().resolve(strict=False)
    repo_policies = list_registered_repo_policies(workspace=workspace, db=db)
    if registered_repo_for_path(candidate, policies=repo_policies) is not None:
        return None
    if candidate != scope_root and not candidate.is_relative_to(scope_root):
        if not candidate.is_relative_to(workspace.resolve(strict=False)):
            return None
        raise DurableWritePolicyError(
            "Ordinary writes must remain inside the current workspace/project scope."
        )
    conductor_id = scope_conductor(authoritative_project, db=db)
    structure_owner = conductor_id == thread_id or direct_user
    parts = candidate.relative_to(scope_root).parts
    if len(parts) <= 1:
        if structure_owner:
            return None
        raise DurableWritePolicyError(structure_owned_message("The scope root", conductor_id))
    if parts[0] == ".threads":
        if len(parts) >= 3 and (parts[1] == thread_id or structure_owner):
            return None
        raise DurableWritePolicyError(
            "A thread may write anywhere inside its own .threads/<id>/ directory, "
            "but not inside another thread's directory."
        )
    if parts[0] != "shared":
        if structure_owner:
            return None
        raise DurableWritePolicyError(structure_owned_message(f"{parts[0]}/", conductor_id))
    return scope_root, candidate, authoritative_project, conductor_id, parts


def _shared_location(
    candidate: Path,
    scope_root: Path,
    parts: tuple[str, ...],
    *,
    structure_owner: bool,
    conductor_id: str | None,
    scope_id: str,
) -> tuple[Path, Path, bool] | SharedTarget:
    """Validate category structure and return its root, index, and create flag."""
    shared_root = scope_root / "shared"
    if len(parts) == 2:
        if parts[1] == "INDEX.md" and structure_owner:
            return SharedTarget(candidate, shared_root, candidate, False, scope_id)
        raise DurableWritePolicyError(
            "Files cannot be written directly inside shared/. "
            "Use an approved shared/<category>/ directory."
        )
    category_root = shared_root / parts[1]
    category_index = category_root / "INDEX.md"
    approved = _approved_categories(shared_root)
    creates_index = candidate == category_index and parts[1] not in approved
    if parts[1] not in approved and not (structure_owner and creates_index):
        raise DurableWritePolicyError(missing_category_message(parts[1], approved, conductor_id))
    return category_root, category_index, creates_index


def _creation_details(
    *,
    existed: bool,
    parts: tuple[str, ...],
    thread_id: str,
    project_id: str | None,
    direct_user: bool,
    requested_owner: str | None,
    update_trigger: str | None,
    db,
) -> tuple[str | None, str | None]:
    if existed:
        return None, None
    dated_trigger = _dated_trigger(parts[2:])
    trigger = dated_trigger if direct_user else (update_trigger or "").strip() or dated_trigger
    if not direct_user and not trigger:
        raise DurableWritePolicyError(
            "An `update_trigger` is required when creating a durable shared file."
        )
    owner = _creation_owner(
        thread_id=thread_id,
        project_id=project_id,
        direct_user=direct_user,
        requested_owner=requested_owner,
        db=db,
    )
    return owner, trigger


def resolve_shared_target(
    target: Path,
    *,
    workspace: Path,
    thread_id: str,
    project_id: str | None,
    description: str | None,
    direct_user: bool,
    db,
    proposed_content: bytes | None = None,
    owner: str | None = None,
    update_trigger: str | None = None,
    setup: bool = True,
    promoted_hash: str | None = None,
) -> SharedTarget | None:
    """Validate one path and apply the shared-document owner gate.

    Returns None for a path outside ``shared/``; a writer that lands one asks
    ``document_write_authority`` for its ownership decision.
    """
    resolved = _shared_candidate(
        target, workspace=workspace, thread_id=thread_id,
        project_id=project_id, direct_user=direct_user, db=db,
    )
    if resolved is None:
        return None
    scope_root, candidate, authoritative_project, conductor_id, parts = resolved
    scope_id = authoritative_project or WORKSPACE_SCOPE
    location = _shared_location(
        candidate, scope_root, parts,
        structure_owner=conductor_id == thread_id or direct_user,
        conductor_id=conductor_id, scope_id=scope_id,
    )
    if isinstance(location, SharedTarget):
        return location
    category_root, category_index, creates_index = location
    existed = candidate.exists()
    if not existed and (not description or not description.strip()) and candidate.name != "INDEX.md":
        raise DurableWritePolicyError("A description is required when creating a durable shared file.")
    creation_owner, trigger = _creation_details(
        existed=existed, parts=parts, thread_id=thread_id,
        project_id=authoritative_project, direct_user=direct_user,
        requested_owner=owner, update_trigger=update_trigger, db=db,
    )
    artifact = recover_moved_identity(
        candidate, thread_id=thread_id, db=db, setup=setup,
    ) if existed else None
    gate = gate_existing_document(
        candidate, proposed_content, scope_root=scope_root, scope_id=scope_id,
        conductor_id=conductor_id, thread_id=thread_id, direct_user=direct_user,
        artifact=artifact, db=db, setup=setup, promoted_hash=promoted_hash,
    ) if existed else (creation_owner, None, False, None, None)
    actor, current_owner, outside_change, classification, authority = gate
    return SharedTarget(
        candidate, category_root, category_index,
        creates_index and not category_root.exists(), scope_id,
        actor_principal=actor, owner_thread_id=current_owner,
        update_trigger=trigger, outside_change=outside_change, classification=classification,
        authority=authority,
    )


def workspace_write_requires_review(
    target: Path,
    *,
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    description: str | None = None,
    owner: str | None = None,
    update_trigger: str | None = None,
) -> bool:
    """Return whether ordinary Workspace governance would reject this write.

    An ownership refusal is raised instead: it is final, never the host reviewer's to waive.
    """
    db = get_db()
    try:
        try:
            resolve_shared_target(
                target,
                workspace=workspace,
                thread_id=thread_id,
                project_id=project_id,
                description=description,
                direct_user=False,
                db=db,
                owner=owner,
                update_trigger=update_trigger,
            )
        except DocumentOwnershipRefused:
            raise
        except DurableWritePolicyError:
            document_write_authority(target, workspace=workspace, thread_id=thread_id, direct_user=False, db=db)
            return True
        return False
    finally:
        db.close()
