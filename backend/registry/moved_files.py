"""Recover one identity after a file was moved outside CORSIA's tools."""

from __future__ import annotations

import logging
from pathlib import Path

from infra.database import write_transaction
from registry import models, service
from registry.file_identity import MovedFileRecovery, external_identity_for_path
from registry.file_resolution import normalize_file_path
from registry.identity_metadata import sha256_file
from registry import relocate
from registry.relocation_identity import target_identity

log = logging.getLogger(__name__)


def _missing_hash_candidates(db, *, zone: str, digest: str) -> list[dict]:
    rows = db.execute(
        """SELECT * FROM artifact_identities
           WHERE state='active' AND integration='workspace' AND path IS NOT NULL
             AND json_extract(CASE WHEN json_valid(metadata) THEN metadata ELSE '{}'
                              END,'$.zone')=?
             AND json_extract(CASE WHEN json_valid(metadata) THEN metadata ELSE '{}'
                              END,'$.last_written_sha256')=?
           ORDER BY id""",
        (zone, digest),
    ).fetchall()
    return [dict(row) for row in rows if not Path(row["path"]).exists()]


def _validated_move(
    db,
    identity: dict,
    target: Path,
    *,
    zone: str,
    actor_thread_id: str,
) -> dict:
    source = Path(identity["path"])
    source_identity = normalize_file_path(str(source), db=db)
    if source_identity is None or source_identity[1] != zone:
        raise relocate.RelocationError("moved file changed registrable zones")
    relocate._reject_foreign_thread_tray(str(target), actor_thread_id)
    target_type, target_title = target_identity(identity, source, target)
    move = {
        "target_path": str(target),
        "target_external_id": external_identity_for_path(str(target), zone, db=db),
        "target_type": target_type,
        "target_title": target_title,
    }
    _reject_collision(db, identity, move)
    return move


def _reject_collision(db, identity: dict, move: dict) -> None:
    collision = models.find_identity_by_external_ids(
        "workspace", move["target_type"], (move["target_external_id"],), db=db,
    )
    if collision is not None and collision["id"] != identity["id"]:
        raise relocate.RelocationError(
            f"target identity already registered: {move['target_external_id']}"
        )


def plan_shared_moved_file_recovery(
    path: Path, actor_thread_id: str, db,
) -> MovedFileRecovery | None:
    """Plan a recovery only for the shared-document surface; reads, never writes."""
    from infra.config import WORKSPACE
    from registry.shared_principals import is_shared_document_path

    if not is_shared_document_path(path, workspace=WORKSPACE, db=db):
        return None
    return plan_moved_file_recovery(path, actor_thread_id=actor_thread_id, db=db)


def plan_moved_file_recovery(
    path: Path,
    *,
    actor_thread_id: str,
    db,
) -> MovedFileRecovery | None:
    """Find the unique missing active identity matching ``path``'s hash."""
    target = path.expanduser().resolve(strict=False)
    if not target.is_file() or target.is_symlink():
        return None
    normalized = normalize_file_path(str(target), db=db)
    if normalized is None:
        return None
    _canonical, zone, _default = normalized
    candidates = _missing_hash_candidates(db, zone=zone, digest=sha256_file(target))
    if len(candidates) != 1:
        if len(candidates) > 1:
            log.warning(
                "moved-file identity recovery is ambiguous for %s; candidates=%s",
                target,
                [candidate["id"] for candidate in candidates],
            )
        return None

    identity = candidates[0]
    move = _validated_move(
        db,
        identity,
        target,
        zone=zone,
        actor_thread_id=actor_thread_id,
    )
    return MovedFileRecovery(identity, move)


def apply_moved_file_recovery(
    plan: MovedFileRecovery,
    *,
    actor_thread_id: str,
    db,
) -> dict | None:
    """Write a planned recovery; database only, so it may run under a writer."""
    identity, move = plan.identity, plan.move
    with write_transaction(db, scope="recover_moved_file_identity") as connection:
        current = connection.execute(
            "SELECT path, state FROM artifact_identities WHERE id=?", (identity["id"],),
        ).fetchone()
        if current is None or current["state"] != "active" or current["path"] != identity["path"]:
            return None
        _reject_collision(connection, identity, move)
        artifact = relocate.write_existing_move(
            identity,
            move,
            actor_thread_id=actor_thread_id,
            db=connection,
        )
        models.record_artifact_change(
            artifact_id=identity["id"],
            internal_event="relocated",
            external_id=move["target_external_id"],
            summary=(
                f"`{identity['path']}` was moved to `{move['target_path']}` "
                "outside the file tools; it is still yours."
            ),
            provenance={
                "reason": "moved_outside_tools",
                "source_path": identity["path"],
                "target_path": move["target_path"],
            },
            actor_thread_id=actor_thread_id,
            db=connection,
        )
    return artifact


def recover_moved_file_identity(
    path: Path,
    *,
    actor_thread_id: str,
    db,
) -> dict | None:
    """Relocate the unique missing active identity matching ``path``'s hash."""
    plan = plan_moved_file_recovery(path, actor_thread_id=actor_thread_id, db=db)
    if plan is None:
        return None
    return apply_moved_file_recovery(plan, actor_thread_id=actor_thread_id, db=db)
