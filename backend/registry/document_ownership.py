"""The ownership gate every document write passes (DCOL-FR5.9).

Only a document's owner, the owner's temporary subagents, the conductor of the
scope that holds it and its temporary subagents, and the user edit it; every
other agent suggests. ``shared_principals.may_edit_document`` decides; this
module finds the owner and scope a write is decided against, and refuses.
"""

from __future__ import annotations

from pathlib import Path

from infra.settings import get_setting
from registry import service
from registry.identity_metadata import last_written_sha256, sha256_file
from registry.shared_document_messages import owner_refusal
from registry.shared_edit_classification import classify_edit
from registry.shared_principals import (
    UI_PRINCIPAL,
    WriteAuthority,
    acting_owner,
    artifact_owner,
    document_scope,
    may_edit_document,
    scope_relative,
)
from registry.shared_write_errors import (
    DocumentOwnershipRefused,
    DurableWritePolicyError,
    SharedIdentitySetupRequired,
)

# The stored key keeps its historical "ownership enforced" name: it is persisted data.
ADOPTION_SETTING_PREFIX = "shared_ownership:"


def conductor_adopts_unowned(scope_id: str, *, db) -> bool:
    """Whether a write to an unowned document registers it to the scope's conductor.

    Refusing a non-owner never depends on this: an owned document refuses every
    non-owner, in every scope.
    """
    setting = get_setting(f"{ADOPTION_SETTING_PREFIX}{scope_id}", db=db) or {}
    return bool(setting.get("shared_ownership_enforced_at"))


def _owner_row(artifact: dict | None, *, db) -> str | None:
    if artifact is None:
        return None
    return artifact_owner(str(artifact["id"]), db=db)


def recover_moved_identity(path: Path, *, thread_id: str, db, setup: bool = True) -> dict | None:
    artifact = service.find_file_identity(str(path), db=db)
    if artifact is not None or not path.is_file() or path.is_symlink():
        return artifact
    from registry.moved_files import plan_moved_file_recovery, apply_moved_file_recovery

    plan = plan_moved_file_recovery(path, actor_thread_id=thread_id, db=db)
    if plan is None:
        return None
    if not setup:
        raise SharedIdentitySetupRequired()
    return apply_moved_file_recovery(plan, actor_thread_id=thread_id, db=db)


def gate_existing_document(
    path: Path,
    proposed_content: bytes | None,
    *,
    scope_root: Path,
    scope_id: str,
    conductor_id: str | None,
    thread_id: str,
    direct_user: bool,
    artifact: dict | None,
    db,
    setup: bool = True,
    promoted_hash: str | None = None,
) -> tuple[str | None, str | None, bool, str | None, WriteAuthority]:
    adopts = conductor_adopts_unowned(scope_id, db=db)
    owner = _owner_row(artifact, db=db)
    if owner is None and adopts:
        if conductor_id is None:
            raise DurableWritePolicyError("Ownership enforcement requires a designated conductor.")
        if not setup:
            raise SharedIdentitySetupRequired()
        artifact = service.register_write_strict(conductor_id, str(path), db=db)
        owner = _owner_row(artifact, db=db) or conductor_id

    actor = acting_owner(thread_id, direct_user, db=db)
    if direct_user and owner is None and thread_id != UI_PRINCIPAL:
        actor = thread_id
    stored_hash = promoted_hash if promoted_hash is not None else last_written_sha256(
        artifact.get("metadata") if artifact else None,
    )
    outside_change = bool(stored_hash and sha256_file(path) != stored_hash)
    classification = None
    if owner is not None and proposed_content is not None:
        classification = classify_edit(path, path.read_bytes(), proposed_content)
    authority = may_edit_document(
        thread_id, direct_user, owner=owner, conductor=conductor_id, path_rel=scope_relative(path, scope_root),
        artifact_id=str(artifact["id"]) if artifact else None, db=db,
        mechanical_index=path.name == "INDEX.md" and classification == "mechanical",
    )
    if not authority.allowed:
        raise DocumentOwnershipRefused(owner_refusal(path, scope_root, owner, db=db))
    return actor, owner, outside_change, classification, authority


def document_write_authority(
    target: Path, *, workspace: Path, thread_id: str, direct_user: bool, db,
) -> WriteAuthority | None:
    """Decide ownership for a write the shared publisher does not govern; raise when refused.

    For a plain path and for a reviewed host write. Only a registered workspace
    file is a document: repository files are code, changed through worktrees
    and pull requests. Returns None when the path is no owned document.
    """
    path = target.expanduser().resolve(strict=False)
    if not path.is_file():
        return None
    identity = service.prepare_file_identity(str(path), db=db)
    if identity is None or identity.zone != "workspace":
        return None
    artifact = service.find_file_identity(identity, db=db)
    owner = _owner_row(artifact, db=db)
    if artifact is None or owner is None:
        return None
    scope_root, conductor = document_scope(path, workspace=workspace, db=db)
    authority = may_edit_document(
        thread_id, direct_user, owner=owner, conductor=conductor, path_rel=scope_relative(path, scope_root),
        artifact_id=str(artifact["id"]), db=db,
    )
    if not authority.allowed:
        raise DocumentOwnershipRefused(owner_refusal(path, scope_root, owner, db=db))
    return authority
