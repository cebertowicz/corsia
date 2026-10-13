"""Registry service for writes, uploads, and pane notifications."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from core.workspace_layout import uploads_root_for_path
from infra.database import TransactionOwnershipError, get_db, write_transaction
from infra.github_pr_identity import GITHUB_INTEGRATION, PR_TYPE
from registry import models, registration_events, registration_writer
from registry.cc_session import notify_cc_session, persist_cc_session
from registry.file_identity import FileIdentity, PreparedFileWrite, external_identity_for_path
from registry.file_types import extension_type, image_media_type
from registry.identity_bridge import OWNER_REGISTRATION_STATES, PANE_REGISTRATION_STATES
from registry.identity_metadata import last_written_sha256, merge_identity_metadata, parse_metadata
from registry.notifications import notify_changed
from registry.observation_owner import require_observable_owner
from registry.origin_telemetry import emit_committed_origin
from registry.upload_registration import register_accepted_upload
from tools.sandbox_config import get_allowed_dirs_rw


def classify(path: Path) -> str:
    """Artifact type for a file."""
    from core.skill_validation import is_skill_file

    if is_skill_file(path):
        return "skill"
    return extension_type(path)


def is_tmp(path: Path) -> bool:
    """True if the file lives under a ``working/`` tray."""
    return "working" in {p.lower() for p in path.parts}


def registrable_zone(p: Path, *, db=None) -> str | None:
    """Return ``workspace`` or ``repo`` when ``p`` is registrable."""
    from infra import config

    workspace = config.WORKSPACE.resolve()
    candidates = [(workspace, "workspace")]
    candidates.extend(
        (root, "repo")
        for root in {
            allowed.resolve()
            for allowed in get_allowed_dirs_rw(db=db, containing=p)
            if allowed.resolve() != workspace
        }
    )
    matches = [
        (root, zone)
        for root, zone in candidates
        if p == root or p.is_relative_to(root)
    ]
    if not matches:
        return None
    return max(matches, key=lambda match: len(match[0].parts))[1]


def normalize_file_path(raw_path: str, *, db=None) -> tuple[str, str, bool] | None:
    """Resolve a caller-supplied file path for registration."""
    from infra import config

    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = config.WORKSPACE / p
    p = p.resolve()
    zone = registrable_zone(p, db=db)
    if zone is None:
        return None
    return str(p), zone, (is_tmp(p) if zone == "workspace" else False)


def register_write(
    thread_id: str | None,
    abs_path: str,
    description: str | None = None,
    *, origin: registration_writer.RegistrationOrigin | None = None,
    metadata: dict[str, Any] | None = None,
    db=None,
    notify: bool = True,
) -> dict[str, Any] | None:
    """Register a confirmed file write to its thread (L5). Best-effort; never
    raises. Returns an artifact identity reference when registration succeeds.

    ``notify=False`` defers the WS nudge to the caller: the broadcast layer
    schedules onto the running event loop, so off-loop callers (the observer
    inside the DB worker) must fire it from their loop-side seam instead.
    """
    if not thread_id:
        return None
    try:
        return register_write_strict(
            thread_id,
            abs_path,
            description, origin=origin, metadata=metadata,
            db=db,
            notify=notify,
        )
    except Exception as e:
        logger.warning(f"registry register_write failed for {abs_path}: {e}")
        return None


def prepare_file_identity(abs_path: str | FileIdentity, *, db=None) -> FileIdentity | None:
    """Prepare filesystem facts once; database ownership is always read later."""
    if isinstance(abs_path, FileIdentity):
        return abs_path
    normalized = normalize_file_path(abs_path, db=db)
    if normalized is None:
        return None
    canonical, zone, ephemeral = normalized
    return FileIdentity(canonical, zone, ephemeral, classify(Path(canonical)),
                        external_identity_for_path(canonical, zone, db=db))


def find_file_identity(abs_path: str | FileIdentity, *, db=None) -> dict[str, Any] | None:
    """Resolve a workspace/repository file logical-first, then by legacy path."""
    identity = prepare_file_identity(abs_path, db=db)
    if identity is None:
        return None
    _db = db or get_db()
    try:
        return models.find_identity_by_external_ids(
            "workspace",
            identity.artifact_type,
            (identity.external_id, identity.canonical),
            db=_db,
        )
    finally:
        if not db:
            _db.close()


def resolve_file_identity(abs_path: str, *, db=None) -> dict[str, Any] | None:
    """Resolve an active file owner from a physical path.

    Workspace paths prefer their relocatable logical identity and retain the
    absolute identity only as a compatibility fallback during cutover. A
    canonical path below a thread uploads root also resolves its active upload
    identity so callers can open a registered upload from its local path.
    """
    identity = prepare_file_identity(abs_path, db=db)
    if identity is None:
        return None
    _db = db or get_db()
    try:
        for candidate in dict.fromkeys((identity.external_id, identity.canonical)):
            artifact = models.resolve(
                "workspace",
                identity.artifact_type,
                candidate,
                db=_db,
            )
            if artifact is not None:
                return artifact
        if uploads_root_for_path(identity.canonical) is None:
            return None
        return registration_events.resolve_upload_path(identity.canonical, db=_db)
    finally:
        if not db:
            _db.close()


def retire_missing_temporary_under(
    root: Path,
    *,
    parent_thread_id: str,
    db=None,
    include_archived: bool = False,
) -> list[str]:
    """Tombstone missing owned paths; purge also finishes already-archived identities."""
    return _retire_missing_temporary(
        {parent_thread_id: root}, db=db, parent_thread_id=parent_thread_id,
        include_archived=include_archived,
    )


def retire_missing_temporary_under_roots(
    roots_by_parent_thread_id: Mapping[str, Path],
    *,
    db=None,
) -> list[str]:
    """Tombstone missing owned ephemeral artifacts below their owners' roots."""
    return _retire_missing_temporary(roots_by_parent_thread_id, db=db)


def _retire_missing_temporary(roots, *, db, parent_thread_id=None, include_archived=False):
    """Inspect files without a writer; only prepared facts cross the write seam."""
    _db = db or get_db()
    if _db.in_transaction:
        raise TransactionOwnershipError("temporary retirement requires a clean connection before filesystem work")
    try:
        scopes = {owner: root.expanduser().resolve(strict=False) for owner, root in roots.items()}
        if not scopes:
            return []
        rows = _temporary_owner_rows(_db, parent_thread_id=parent_thread_id,
                                     include_archived=include_archived)
        missing = _missing_temporary_rows(rows, scopes)
        return models.retire_missing_temporary_candidates(missing, db=_db)
    finally:
        if not db:
            _db.close()


def _temporary_owner_rows(db, *, parent_thread_id=None, include_archived=False):
    state_placeholders = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    parent_clause = "AND r.parent_thread_id=?" if parent_thread_id is not None else ""
    parent_params = (parent_thread_id,) if parent_thread_id is not None else ()
    lifecycle_clause = "i.state IN ('active', 'archived')" if include_archived else "i.state='active'"
    return db.execute(
        f"""SELECT DISTINCT i.id, i.path, r.parent_thread_id,
                           i.updated_at, i.state, i.retention
            FROM artifact_identities i
            JOIN artifact_registrations r ON r.artifact_id=i.id
            WHERE {lifecycle_clause}
              AND i.retention='ephemeral'
              AND i.path IS NOT NULL
              AND i.path != ''
              {parent_clause}
              AND r.relationship_state IN ({state_placeholders})
            ORDER BY i.id, r.parent_thread_id""",
        (*parent_params, *OWNER_REGISTRATION_STATES),
    ).fetchall()


def _missing_temporary_rows(rows, scopes: Mapping[str, Path]) -> list[dict]:
    missing: list[dict] = []
    for row in rows:
        raw_path = row["path"]
        if "://" in raw_path:
            continue
        scope = scopes.get(row["parent_thread_id"])
        if scope is None:
            continue
        candidate = Path(raw_path).expanduser().resolve(strict=False)
        if candidate != scope and not candidate.is_relative_to(scope):
            continue
        if not candidate.exists():
            missing.append(dict(row))
    return missing


def register_write_strict(
    thread_id: str,
    abs_path: str | FileIdentity | PreparedFileWrite,
    description: str | None = None, *,
    title: str | None = None, retention: str | None = None,
    reject_retention_change: bool = False,
    metadata: dict[str, Any] | None = None,
    origin: registration_writer.RegistrationOrigin | None = None, db=None,
    notify: bool = False,
    actor_thread_id: str | None = None,
) -> dict[str, Any]:
    """Register one confirmed write without hiding identity/transaction errors.

    Workspace files use a relocatable ``workspace://`` external identity and
    keep their current resolved location in ``path``. A legacy absolute
    identity is upgraded in place so its artifact id and ownership survive.
    Repository artifacts retain their absolute identity contract.

    ``actor_thread_id`` names who made the edit when that is not the registering
    thread; reviewers waiting on the document are told of a changed digest.
    """
    if not thread_id:
        raise ValueError("a current CORSIA thread is required")
    if db is None and get_db().in_transaction:
        raise TransactionOwnershipError("file registration requires an explicit transaction owner")
    if notify and db is not None and db.in_transaction:
        raise ValueError("notification requires a committed registration")
    from registry.write_preparation import apply_prepared_recovery, prepare_file_write

    prepared = prepare_file_write(abs_path, thread_id, db=db)
    if prepared is None:
        raise ValueError(f"path is outside the registrable roots: {abs_path}")
    identity = prepared.identity
    with write_transaction(db, scope="register_file_write") as _db:
        existing = find_file_identity(identity, db=_db)
        if existing is None:
            existing = apply_prepared_recovery(prepared, thread_id, _db)
        if existing is not None and existing["external_id"] != identity.external_id:
            _db.execute(
                "UPDATE artifact_identities SET external_id=?, path=? WHERE id=?",
                (identity.external_id, identity.canonical, existing["id"]),
            )
        artifact_metadata = prepared.metadata({**(metadata or {}), "zone": identity.zone},
                                               parse_metadata(existing["metadata"]) if existing else {})
        artifact = models.register_artifact(
            integration="workspace",
            type=identity.artifact_type,
            external_id=identity.external_id,
            parent_thread_id=thread_id,
            title=title or Path(identity.canonical).name,
            description=description,
            path=identity.canonical,
            retention=retention,
            retention_default=identity.ephemeral_default,
            reject_retention_change=reject_retention_change,
            metadata=artifact_metadata, origin=origin,
            db=_db,
        )
        merge_identity_metadata(_db, artifact["id"], artifact_metadata)
        artifact = models.get_artifact(artifact["id"], db=_db) or artifact
        if existing is not None:
            from registry.review_edit_journal import journal_reviewed_edit

            journal_reviewed_edit(
                _db, artifact, previous_sha256=last_written_sha256(existing["metadata"]),
                new_sha256=artifact_metadata.get("last_written_sha256"),
                actor_thread_id=thread_id if actor_thread_id is None else actor_thread_id,
            )
    if origin is not None and (db is None or not db.in_transaction):
        emit_committed_origin(db, existing["id"] if existing else artifact["id"], thread_id)
    if notify:
        notify_changed(artifact)
    return artifact


def _observation_is_current(db, artifact_id: str, thread_id: str) -> bool:
    placeholders = ",".join("?" for _ in PANE_REGISTRATION_STATES)
    return db.execute(
        "SELECT 1 FROM artifact_registrations "
        "WHERE artifact_id=? AND parent_thread_id=? "
        f"AND relationship_state IN ({placeholders}) AND surfacing<>'settled'",
        (artifact_id, thread_id, *PANE_REGISTRATION_STATES),
    ).fetchone() is not None


def register_observed_artifact(
    thread_id: str,
    *,
    integration: str,
    type: str,
    external_id: str,
    title: str | None = None,
    path: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    origin: registration_writer.RegistrationOrigin | None = None, db=None,
    notify: bool = True,
) -> dict[str, Any] | None:
    """Register a connector-observed artifact without stealing ownership.

    Returns None when this thread already has an active registration so
    repeated observation across immediate and persisted-result seams is quiet.
    """
    if not thread_id:
        raise ValueError("a current CORSIA thread is required")
    if notify and db is not None and db.in_transaction:
        raise ValueError("notification requires a committed registration")
    with write_transaction(db, scope="register_observed_artifact") as _db:
        existing = models.resolve(integration, type, external_id, db=_db)
        require_observable_owner(_db, thread_id, external_id, existing)
        if existing is not None and _observation_is_current(
            _db, existing["id"], thread_id
        ):
            return None

        artifact = models.register_artifact(
            integration=integration,
            type=type,
            external_id=external_id,
            parent_thread_id=thread_id,
            title=title,
            path=path,
            metadata=dict(metadata) if metadata else None, origin=origin,
            db=_db,
        )
    if origin is not None and (db is None or not db.in_transaction):
        identity = artifact or models.find_identity_by_external_ids(
            integration, type, (external_id,), db=db)
        emit_committed_origin(db, identity["id"], thread_id)
    logger.info(
        "registry: auto-registered observed artifact {}/{}/{} → thread {}",
        integration,
        type,
        external_id,
        thread_id,
    )
    if notify:
        notify_changed(artifact)
    return artifact


def register_github_pr(
    thread_id: str,
    repo: str,
    pr_number: int,
    *,
    db=None,
    notify: bool = True,
) -> dict[str, Any]:
    """Compatibility wrapper for one confirmed GitHub PR observation."""
    if pr_number <= 0:
        raise ValueError("pull-request number must be positive")
    external_id = f"{repo}#{pr_number}"
    artifact = register_observed_artifact(
        thread_id,
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=external_id,
        title=f"PR #{pr_number}",
        path=f"https://github.com/{repo}/pull/{pr_number}",
        db=db,
        notify=notify,
    )
    if artifact is not None:
        return artifact
    _db = db or get_db()
    try:
        existing = models.resolve(GITHUB_INTEGRATION, PR_TYPE, external_id, db=_db)
    finally:
        if not db:
            _db.close()
    if existing is None:
        raise RuntimeError(f"observed PR registration disappeared: {external_id}")
    return existing


def register_upload(
    thread_id: str,
    *,
    file_id: str,
    filename: str,
    mime_type: str,
    size: int,
    path: Path,
) -> dict[str, Any]:
    """Register one durable thread upload at its canonical physical path."""
    canonical = path.resolve(strict=True)
    if not canonical.is_file():
        raise ValueError(f"upload path is not a file: {canonical}")
    row = models.register_artifact(
        integration="upload",
        type="image" if image_media_type(canonical) is not None else "upload",
        external_id=file_id,
        parent_thread_id=thread_id,
        title=filename,
        description=filename,
        path=str(canonical),
        retention=models.RETENTION_DURABLE,
        metadata={"mime": mime_type, "size": size},
    )
    try:
        notify_changed(row)
    except Exception as exc:
        logger.warning(f"registry upload notification failed for {file_id}: {exc}")
    return row


def register_uploads(
    thread_id: str | None,
    attachments: list[dict],
    *,
    source_input_id: str | None = None,
) -> None:
    """Register accepted uploads without replacing a canonical local path."""
    if not thread_id or not attachments:
        return
    try:
        for att in attachments:
            fid = att.get("file_id") or att.get("url") or att.get("filename")
            if not fid:
                continue
            row = register_accepted_upload(
                thread_id, att, str(fid), source_input_id=source_input_id,
            )
            notify_changed(row or {
                "integration": "upload",
                "type": "upload",
                "external_id": str(fid),
            })
    except Exception as e:
        logger.warning(f"registry register_uploads failed: {e}")



def register_cc_session(
    thread_id: str | None, cc_session_id: str | None,
    *,
    title: str | None = None, cwd: str | None = None,
    invocation: object | None = None, checkpoint_generation_id: str | None = None,
    expected_previous_id: str | None = None,
    verify_previous: bool = False,
    db=None,
) -> str | None:
    """Atomically replace a thread's current CC continuity artifact.

    The durable write is loud; only its UI notification is best-effort.
    """
    previous_id, row = persist_cc_session(
        thread_id, cc_session_id, title=title, cwd=cwd, invocation=invocation,
        checkpoint_generation_id=checkpoint_generation_id, expected_previous_id=expected_previous_id,
        verify_previous=verify_previous, db=db,
    )
    if db is None or not db.in_transaction:
        notify_cc_session(thread_id, cc_session_id, row)
    return previous_id


def register_codex_thread(
    thread_id: str | None, codex_thread_id: str,
    *,
    rollout_path: str | None = None, tool_surface_fingerprint: str | None = None,
    reasoning_effort: str | None = None, checkpoint_generation_id: str | None = None,
    db=None,
) -> dict[str, Any] | None:
    """Atomically register one Codex handle and its exact launch metadata."""
    if not thread_id:
        return None
    with write_transaction(db, scope="register_codex_thread") as connection:
        row = models.register_artifact(
            integration="codex",
            type="thread",
            external_id=codex_thread_id,
            parent_thread_id=thread_id,
            db=connection,
        )
        metadata = {}
        if rollout_path:
            metadata["rollout_path"] = rollout_path
        if tool_surface_fingerprint:
            metadata["tool_surface_fingerprint"] = tool_surface_fingerprint
        if reasoning_effort is not None:
            metadata["reasoning_effort"] = str(reasoning_effort).strip()
        if checkpoint_generation_id is not None:
            metadata["checkpoint_generation_id"] = checkpoint_generation_id
        if metadata:
            # register_artifact never rewrites metadata on an existing
            # identity; the merge covers fresh and re-registered rows alike.
            row = models.update_metadata(
                "codex",
                "thread",
                codex_thread_id,
                metadata,
                db=connection,
            ) or row
        return row or {
            "integration": "codex",
            "type": "thread",
            "external_id": codex_thread_id,
        }
