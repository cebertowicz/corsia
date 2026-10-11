"""Atomic shared-file publication through the existing artifact registry."""

from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import logging
from pathlib import Path
import re
from uuid import uuid4

from core.workspace_layout import resolve_project_root, resolve_thread_working_root
from infra.database import TransactionOwnershipError, get_db, write_transaction
from registry.shared_category_index import (
    _index_lists_file,
    append_category_index_entry,
)
from registry.shared_document_events import record_post_write_events
from registry.identity_metadata import sha256_file as _sha256_file
from registry import durable_write_intents as intents
from registry import file_resolution, models, registration_writer, service, write_preparation
from registry.shared_principals import WriteAuthority, artifact_owner
from registry.shared_target import SharedTarget
from registry.document_ownership import document_write_authority
from registry.shared_documents import _scope_root, resolve_shared_target as _shared_target
from registry.shared_write_errors import DurableWritePolicyError
from tools.filesystem_effects import (
    bound_file_mode,
    claim_bound_file,
    copy_bound_file,
    replace_bound_file,
    restore_claimed_file,
    stage_bound_bytes,
    stage_bound_file,
    unlink_bound_file,
)


_INTENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
logger = logging.getLogger(__name__)


# Shared target policy is implemented in registry.shared_documents.


def _remove_if_present(path: Path | None) -> None:
    if path is None:
        return
    if not path.parent.exists():
        return
    try:
        unlink_bound_file(path)
    except FileNotFoundError:
        return


def _remove_created_parent(path: Path) -> None:
    """Remove an unpublished category directory, failing on foreign contents."""
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"Unpublished shared category contains unexpected files: {path}"
        ) from exc


def _restore_target(target: Path, backup: Path | None) -> None:
    if backup is None:
        _remove_if_present(target)
        return
    if backup.exists():
        rollback_stage = backup.with_name(
            f"{backup.name.removesuffix('.backup')}.stage"
        )
        copy_bound_file(backup, rollback_stage)
        replace_bound_file(rollback_stage, target)


def _finish_undo(
    target: Path,
    backup: Path | None,
    source: Path | None,
    claim: Path | None,
    intent_id: str,
    content_hash: str,
    *,
    created_parent: bool,
) -> Path | None:
    """Idempotently finish one durably announced filesystem undo."""
    if backup is None:
        if target.exists():
            if not target.is_file() or _sha256_file(target) != content_hash:
                raise RuntimeError(
                    f"Durable write intent {intent_id} no longer owns its target."
                )
            _restore_target(target, None)
    elif backup.exists():
        _restore_target(target, backup)
    else:
        raise RuntimeError(
            f"Durable write intent {intent_id} lost its rollback backup."
        )

    recovered_source = _recover_unpublished_claim(claim, source, intent_id)
    if created_parent:
        _remove_created_parent(target.parent)
    return recovered_source


def _cleanup_undo(stage: Path, backup: Path | None) -> None:
    """Remove rollback evidence only after undo completion is durable."""
    _remove_if_present(stage)
    _remove_if_present(backup)


def _bounded_shared_files(shared_root: Path, *, limit: int = 2000):
    pending = [shared_root]
    visited = 0
    while pending and visited < limit:
        current = pending.pop()
        try:
            children = sorted(
                current.iterdir(),
                key=lambda path: path.name.casefold(),
            )
        except OSError:
            continue
        directories: list[Path] = []
        for candidate in children:
            visited += 1
            if visited > limit:
                break
            try:
                if candidate.is_symlink():
                    continue
                if candidate.is_dir():
                    directories.append(candidate)
                elif candidate.is_file():
                    yield candidate
            except OSError:
                continue
        pending.extend(reversed(directories))


def _similar_files(target: Path, shared_root: Path) -> list[str]:
    scored: list[tuple[float, str]] = []
    target_stem = target.stem.casefold()
    for candidate in _bounded_shared_files(shared_root):
        if (
            candidate == target
            or candidate.name == "INDEX.md"
            or candidate.name.startswith(".")
        ):
            continue
        score = SequenceMatcher(None, target_stem, candidate.stem.casefold()).ratio()
        if score >= 0.45:
            scored.append((score, str(candidate)))
    scored.sort(key=lambda item: (-item[0], item[1].casefold()))
    return [path for _score, path in scored[:5]]


def _intent_identity(target: Path, *, db) -> tuple[str, str]:
    identity = file_resolution.prepare_file_identity(str(target), db=db)
    if identity is None:
        raise DurableWritePolicyError(
            f"Shared target is outside registrable roots: {target}"
        )
    return identity.artifact_type, identity.external_id


def _source_claim(source: Path | None, intent_id: str) -> Path | None:
    if source is None:
        return None
    return source.parent / f".corsia-write-{intent_id}.source"


def _recover_unpublished_claim(
    claim: Path | None,
    source: Path | None,
    intent_id: str,
) -> Path | None:
    """Recover an unpublished claim without replacing or losing newer work."""
    if claim is None or source is None:
        return None
    try:
        restored = restore_claimed_file(claim, source)
    except FileNotFoundError:
        return None
    if restored:
        return source
    collision = source.with_name(f"{source.name}.promotion-recovery-{intent_id}")
    if restore_claimed_file(claim, collision):
        return collision
    return claim


def _register_applied(
    shared: SharedTarget,
    source: Path | None,
    *,
    thread_id: str,
    description: str | None,
    external_id: str,
    content_hash: str,
    before: bytes | None,
    db,
    origin: registration_writer.RegistrationOrigin | None = None,
) -> dict:
    """Register the applied bytes, metadata, and owner events atomically."""
    registration_owner = shared.actor_principal or thread_id
    prepared = write_preparation.prepare_file_write(str(shared.path), registration_owner, db=db)
    if prepared is None:
        raise DurableWritePolicyError(
            f"Shared target is outside registrable roots: {shared.path}"
        )
    source_identity = (
        file_resolution.prepare_file_identity(str(source), db=db)
        if source is not None and not source.exists() else None
    )
    metadata = {"last_written_sha256": content_hash}
    if shared.update_trigger:
        metadata["update_trigger"] = shared.update_trigger
    with write_transaction(db, scope="register_durable_write"):
        artifact = service.register_write_strict(
            registration_owner,
            prepared,
            description,
            metadata=metadata,
            origin=origin,
            db=db,
        )
        if artifact["external_id"] != external_id:
            raise RuntimeError("Durable write identity changed during registration.")
        source_artifact = (
            file_resolution.find_file_identity(source_identity, db=db)
            if source_identity is not None else None
        )
        if source_artifact is not None:
            models.retire(source_artifact["id"], tombstone=True, db=db)
        record_post_write_events(
            shared,
            artifact,
            before=before,
            after=shared.path.read_bytes(),
            actor_thread_id=thread_id,
            db=db,
        )
    return artifact


def _validated_intent_paths(
    row: dict,
    *,
    workspace: Path,
    db,
) -> tuple[Path, Path, Path | None, Path | None, Path | None]:
    """Rebuild only the exact paths a write intent is allowed to affect."""
    intent_id = str(row["id"])
    if _INTENT_ID_RE.fullmatch(intent_id) is None:
        raise RuntimeError("Durable write intent has an invalid identifier.")
    target = Path(row["target_path"]).resolve(strict=False)
    if not target.is_relative_to(workspace):
        raise RuntimeError(
            f"Durable write intent {row['id']} escapes the selected workspace."
        )
    stage = Path(row["stage_path"]).resolve(strict=False)
    expected_stage = (target.parent / f".corsia-write-{intent_id}.stage").resolve(
        strict=False
    )
    if stage != expected_stage:
        raise RuntimeError(
            f"Durable write intent {row['id']} has an invalid stage path."
        )
    backup = (
        Path(row["backup_path"]).resolve(strict=False) if row["backup_path"] else None
    )
    expected_backup = (target.parent / f".corsia-write-{intent_id}.backup").resolve(
        strict=False
    )
    if backup is not None and backup != expected_backup:
        raise RuntimeError(
            f"Durable write intent {row['id']} has an invalid backup path."
        )

    source = (
        Path(row["source_path"]).resolve(strict=False) if row["source_path"] else None
    )
    if row["operation"] == "write" and source is not None:
        raise RuntimeError(
            f"Durable write intent {row['id']} has an invalid source path."
        )
    if row["operation"] == "promote":
        session = db.execute(
            "SELECT project_id FROM threads WHERE id=?",
            (row["actor_thread_id"],),
        ).fetchone()
        if session is None or source is None:
            raise RuntimeError(
                f"Durable promotion intent {row['id']} lost its owning thread."
            )
        scope_root, _project_id = _scope_root(
            workspace=workspace,
            thread_id=row["actor_thread_id"],
            project_id=session["project_id"],
            direct_user=False,
            db=db,
        )
        working_root = resolve_thread_working_root(
            scope_root,
            row["actor_thread_id"],
        ).resolve(strict=False)
        if source == working_root or not source.is_relative_to(working_root):
            raise RuntimeError(
                f"Durable promotion intent {row['id']} has an invalid source path."
            )

    artifact_type, external_id = _intent_identity(target, db=db)
    if artifact_type != row["artifact_type"] or external_id != row["external_id"]:
        raise RuntimeError(
            f"Durable write intent {row['id']} has an invalid artifact identity."
        )

    if row["created_parent"]:
        category_root = target.parent
        scope_root = category_root.parent.parent
        valid_scope = scope_root == workspace
        if not valid_scope:
            project_rows = db.execute("SELECT id FROM projects").fetchall()
            valid_scope = any(
                resolve_project_root(workspace, project["id"], db=db) == scope_root
                for project in project_rows
            )
        if (
            row["operation"] != "write"
            or source is not None
            or backup is not None
            or target.name != "INDEX.md"
            or category_root.parent.name != "shared"
            or not valid_scope
        ):
            raise RuntimeError(
                f"Durable write intent {row['id']} has an invalid created parent."
            )
    return target, stage, backup, source, _source_claim(source, intent_id)


def _register_in_category_index(
    shared: SharedTarget,
    description: str | None,
    *,
    thread_id: str | None = None,
    direct_user: bool = False,
    db=None,
) -> str | None:
    return append_category_index_entry(
        shared, description, thread_id=thread_id, direct_user=direct_user,
        db=db, index_lookup=_index_lists_file,
    )


def _complete_publication(
    shared: SharedTarget,
    artifact: dict,
    *,
    intent_id: str,
    stage: Path,
    backup: Path | None,
    claim: Path | None,
    description: str | None,
    thread_id: str,
    direct_user: bool,
    db,
    notify: bool,
) -> dict:
    """Clean committed intent residue, update the index, and build the receipt."""
    try:
        _remove_if_present(stage)
        _remove_if_present(backup)
        _remove_if_present(claim)
        intents.delete(db, intent_id)
    except Exception:
        logger.warning(
            "Durable write %s committed with cleanup pending for startup recovery.",
            intent_id,
            exc_info=True,
        )
    index_error = None
    if backup is None:
        # Only a newly created file needs an index entry; edits already have one.
        index_error = _register_in_category_index(
            shared,
            description,
            thread_id=thread_id,
            direct_user=direct_user,
            db=db,
        )
        if index_error is not None:
            logger.warning(
                "Durable write %s published but its category index was not "
                "updated: %s",
                intent_id,
                index_error,
            )
    similar = _similar_files(shared.path, shared.category_root.parent)
    if notify:
        service.notify_changed(artifact)
    result_index: dict[str, str] = (
        {"index_error": index_error}
        if index_error is not None
        else {"index_registered": str(shared.index_path)}
    )
    return {
        "ok": True,
        "path": str(shared.path),
        "artifact": artifact,
        **result_index,
        "similar_files": similar,
        "deduplication_note": (
            "Check similar files before creating duplicate truth. When this file "
            "supersedes another, consolidate retained material and retire the old file."
        ),
        "registry_managed": True,
    }


def _publish_bytes(
    shared: SharedTarget,
    content: bytes | None,
    *,
    thread_id: str,
    description: str | None,
    db,
    operation: str,
    direct_user: bool = False,
    source: Path | None = None, origin: registration_writer.RegistrationOrigin | None = None,
    notify: bool = True,
) -> dict:
    if (source is None) == (content is None):
        raise ValueError(
            "A durable publication requires bytes or one promotion source."
        )
    artifact_type, external_id = _intent_identity(shared.path, db=db)
    intent_id = str(uuid4())
    claim = _source_claim(source, intent_id)
    content_hash = hashlib.sha256(content).hexdigest() if content is not None else ""
    stage = shared.path.parent / f".corsia-write-{intent_id}.stage"
    before = shared.path.read_bytes() if shared.path.exists() else None
    backup = (
        shared.path.parent / f".corsia-write-{intent_id}.backup"
        if shared.path.exists()
        else None
    )
    intents.create(
        db,
        dict(
            id=intent_id, operation=operation,
            actor_thread_id=shared.actor_principal or thread_id,
            artifact_type=artifact_type, external_id=external_id,
            target_path=str(shared.path), stage_path=str(stage),
            backup_path=str(backup) if backup else None,
            source_path=str(source) if source else None, description=description,
            content_sha256=content_hash, created_parent=int(shared.create_parent),
        ),
    )

    file_applied = False
    parent_created = False
    source_mode: int | None = None
    try:
        if shared.create_parent:
            shared.category_root.mkdir(parents=False, exist_ok=False)
            parent_created = True
        if source is not None:
            assert claim is not None
            source_mode = claim_bound_file(source, claim)
            _staged, content_hash, _size = stage_bound_file(
                claim,
                shared.path,
                leaf=stage.name,
                mode=source_mode,
            )
            intents.record_source_hash(db, intent_id, content_hash)
        else:
            assert content is not None
            target_mode = bound_file_mode(shared.path) if backup is not None else 0o600
            stage_bound_bytes(
                shared.path,
                content,
                leaf=stage.name,
                mode=target_mode,
            )
        if backup is not None:
            copy_bound_file(shared.path, backup)
            replace_bound_file(stage, shared.path)
        elif not restore_claimed_file(stage, shared.path):
            raise FileExistsError(
                f"Publication destination already exists: {shared.path}"
            )
        file_applied = True
        intents.mark_applied(db, intent_id)
        artifact = _register_applied(
            shared,
            source,
            thread_id=thread_id,
            description=description,
            external_id=external_id,
            content_hash=content_hash,
            before=before,
            db=db,
            origin=origin,
        )
    except Exception as exc:
        if file_applied:
            intents.mark_undoing(db, intent_id, 1)
            recovered_source = _finish_undo(
                shared.path,
                backup,
                source,
                claim,
                intent_id,
                content_hash,
                created_parent=parent_created,
            )
            intents.mark_undoing(db, intent_id, 2)
            _cleanup_undo(stage, backup)
        else:
            recovered_source = _recover_unpublished_claim(claim, source, intent_id)
            _remove_if_present(stage)
            _remove_if_present(backup)
            if parent_created:
                _remove_created_parent(shared.category_root)
        if claim is None or not claim.exists():
            intents.delete(db, intent_id)
        if recovered_source not in (None, source):
            raise RuntimeError(
                f"{exc}; durable promotion also encountered a newer source, so "
                f"the claimed version was preserved at {recovered_source}."
            ) from exc
        raise

    return _complete_publication(
        shared,
        artifact,
        intent_id=intent_id,
        stage=stage,
        backup=backup,
        claim=claim,
        description=description,
        thread_id=thread_id,
        direct_user=direct_user,
        db=db,
        notify=notify,
    )


def write_text(
    target: Path,
    content: str,
    *,
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    description: str | None = None,
    owner: str | None = None,
    update_trigger: str | None = None,
    reviewed: bool = False,
    direct_user: bool = False,
    origin: registration_writer.RegistrationOrigin | None = None,
    notify: bool = True,
) -> dict | WriteAuthority | None:
    """Publish governed shared text; for any other write, return the ownership decision to report.

    An ordinary path returns the caller's ``WriteAuthority``, or None when it is no
    owned document. A reviewed host permit is the authority for its exact effect:
    workspace organization is ordinary-policy guidance, not a hidden second veto,
    but document ownership still refuses.
    """
    db = get_db()
    if db.in_transaction:
        raise TransactionOwnershipError("durable publication requires a clean connection")
    try:
        encoded = content.encode("utf-8")
        shared = None if reviewed else _shared_target(
            target,
            workspace=workspace,
            thread_id=thread_id,
            project_id=project_id,
            description=description,
            direct_user=direct_user,
            db=db,
            proposed_content=encoded,
            owner=owner,
            update_trigger=update_trigger,
        )
        if shared is None:
            return document_write_authority(
                target, workspace=workspace, thread_id=thread_id, direct_user=direct_user, db=db,
            )
        return _publish_bytes(
            shared,
            encoded,
            thread_id=thread_id,
            description=description,
            db=db,
            operation="write", direct_user=direct_user,
            origin=origin, notify=notify,
        )
    finally:
        db.close()


def promote_file(
    source: Path,
    category: str,
    *,
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    description: str | None,
    owner: str | None = None,
    update_trigger: str | None = None,
    origin: registration_writer.RegistrationOrigin | None = None,
    notify: bool = True,
) -> dict:
    """Move one exact working-tray file into an approved shared category."""
    if not isinstance(category, str) or not category or Path(category).name != category:
        raise DurableWritePolicyError(
            "Promotion requires one approved shared category name."
        )
    db = get_db()
    if db.in_transaction:
        raise TransactionOwnershipError("durable promotion requires a clean connection")
    try:
        scope_root, _authoritative_project = _scope_root(
            workspace=workspace,
            thread_id=thread_id,
            project_id=project_id,
            direct_user=False,
            db=db,
        )
        working_root = resolve_thread_working_root(scope_root, thread_id).resolve(
            strict=False
        )
        source_input = source.expanduser()
        if not source_input.is_absolute():
            source_input = workspace / source_input
        source_path = source_input.resolve(strict=False)
        if not source_path.is_file() or source_path.is_symlink():
            raise DurableWritePolicyError(
                f"Promotion source is not a regular file: {source}"
            )
        if source_path == working_root or not source_path.is_relative_to(working_root):
            raise DurableWritePolicyError(
                "Only a file under the exact owning thread working root can be promoted."
            )
        target = scope_root / "shared" / category / source_path.name
        if target.exists():
            raise DurableWritePolicyError(
                f"Promotion destination already exists: {target}"
            )
        shared = _shared_target(
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
        if shared is None:  # pragma: no cover - target is constructed under shared/
            raise DurableWritePolicyError(
                "Promotion destination is not durable shared scope."
            )
        return _publish_bytes(
            shared,
            None,
            thread_id=thread_id,
            description=description,
            db=db,
            operation="promote", source=source_path,
            origin=origin, notify=notify,
        )
    finally:
        db.close()


def _recovery_shared_target(target: Path, row: dict, *, workspace: Path, db) -> SharedTarget:
    """Rebuild the policy decision already persisted in an interrupted intent."""
    actor = str(row["actor_thread_id"])
    scope_id = "workspace"
    for project in db.execute("SELECT id FROM projects").fetchall():
        project_root = resolve_project_root(workspace, project["id"], db=db)
        if project_root is not None and target.is_relative_to(
            project_root.resolve(strict=False)
        ):
            scope_id = str(project["id"])
            break
    category_root = target.parent
    artifact = file_resolution.find_file_identity(str(target), db=db)
    owner = artifact_owner(str(artifact["id"]), db=db) if artifact is not None else None
    return SharedTarget(
        target,
        category_root,
        category_root / "INDEX.md",
        bool(row["created_parent"]),
        scope_id,
        actor_principal=str(owner or actor),
        owner_thread_id=str(owner) if owner else None,
    )


def _recover_one_intent(row: dict, *, workspace: Path, db) -> None:
    target, stage, backup, source, claim = _validated_intent_paths(
        row, workspace=workspace, db=db,
    )
    if row["undoing"]:
        recovered_source = (
            _finish_undo(
                target, backup, source, claim, row["id"], row["content_sha256"],
                created_parent=bool(row["created_parent"]),
            )
            if row["undoing"] == 1
            else _recover_unpublished_claim(claim, source, row["id"])
        )
        if row["undoing"] == 1:
            intents.mark_undoing(db, row["id"], 2)
        if claim is not None and claim.exists():
            raise RuntimeError(
                f"Durable promotion intent {row['id']} could not recover its claimed source from {claim}."
            )
        if recovered_source not in (None, source):
            logger.warning("Recovered promotion source collision at %s.", recovered_source)
        _cleanup_undo(stage, backup)
        intents.delete(db, row["id"])
        return
    applied = target.is_file() and _sha256_file(target) == row["content_sha256"]
    if row["phase"] == "staged" and not applied:
        recovered_source = _recover_unpublished_claim(claim, source, row["id"])
        if claim is not None and claim.exists():
            raise RuntimeError(
                f"Durable promotion intent {row['id']} could not recover its claimed source from {claim}."
            )
        if recovered_source not in (None, source):
            logger.warning("Recovered promotion source collision at %s.", recovered_source)
        _remove_if_present(stage)
        _remove_if_present(backup)
        if row["created_parent"]:
            _remove_created_parent(target.parent)
        intents.delete(db, row["id"])
        return
    if not applied:
        raise RuntimeError(
            f"Durable write intent {row['id']} does not match its target bytes."
        )
    shared = _recovery_shared_target(target, row, workspace=workspace, db=db)
    artifact = _register_applied(
        shared,
        source,
        thread_id=row["actor_thread_id"],
        description=row["description"],
        external_id=row["external_id"],
        content_hash=row["content_sha256"],
        before=backup.read_bytes() if backup and backup.exists() else None,
        db=db,
    )
    try:
        _remove_if_present(stage)
        _remove_if_present(backup)
        _remove_if_present(claim)
        intents.delete(db, row["id"])
    except Exception:
        logger.warning(
            "Recovered durable write %s with cleanup still pending.",
            row["id"],
            exc_info=True,
        )
    service.notify_changed(artifact)


def recover_write_intents(*, workspace: Path | None = None) -> None:
    """Reconcile interrupted durable publications before serving new work."""
    from infra import config

    db = get_db()
    if db.in_transaction:
        raise TransactionOwnershipError("durable recovery requires a clean connection")
    try:
        workspace_root = (workspace or config.WORKSPACE).resolve(strict=False)
        rows = db.execute(
            "SELECT * FROM durable_write_intents ORDER BY created_at,id"
        ).fetchall()
        for raw in rows:
            _recover_one_intent(dict(raw), workspace=workspace_root, db=db)
    finally:
        db.close()
