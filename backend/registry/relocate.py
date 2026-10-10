"""Identity-preserving artifact relocation.

Durable intents protect explicit filesystem moves; an already-completed move
uses the same identity and audit writer without changing ownership.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from core.workspace_layout import thread_tray_owners
from infra.database import TransactionOwnershipError, get_db, write_transaction
from registry import models, service
from registry.file_identity import external_identity_for_path
from registry.identity_bridge import OWNER_REGISTRATION_STATES
from registry.registration_events import _append_registration_event, _now
from registry.relocation_identity import target_identity
from registry.shared_principals import is_scope_conductor_for_shared_path


class RelocationError(ValueError):
    """A relocation request failed validation or cannot be completed."""


def _identity_row(db, artifact_id: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM artifact_identities WHERE id=?", (artifact_id,)
    ).fetchone()
    return dict(row) if row else None


def _owner_thread(db, artifact_id: str) -> str | None:
    placeholders = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    row = db.execute(
        f"""SELECT parent_thread_id FROM artifact_registrations
            WHERE artifact_id=? AND relationship_state IN ({placeholders})
            ORDER BY CASE relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                     updated_at DESC LIMIT 1""",
        (artifact_id, *OWNER_REGISTRATION_STATES),
    ).fetchone()
    return row["parent_thread_id"] if row else None


def _reject_foreign_thread_tray(target_canonical: str, actor_thread_id: str) -> None:
    """Refuse a target inside another thread's private ``.threads/<id>/`` tray.

    Registrability is not sufficient authority here: relocation *writes*, and
    every thread's tray is registrable. Anywhere else inside the registrable
    roots — the actor's own tray, project/shared roots, a repository the actor
    may already write — stays allowed, matching the ordinary durable-write
    contract in ``registry.durable_writes``.
    """
    for owner in thread_tray_owners(target_canonical):
        if owner != actor_thread_id:
            raise RelocationError(
                "target is inside another thread's private directory "
                f"(.threads/{owner}/): {target_canonical}"
            )


def _authorized_identity(db, artifact_id: str, actor_thread_id: str) -> dict:
    """Return the identity this thread may relocate, or raise.

    Non-workspace integrations are refused for the same reason as a
    cross-device target: no safe contract exists yet. Every later step is
    workspace-shaped — ``classify`` only returns workspace document/code/skill
    and ``external_identity_for_path`` mints a ``workspace://`` key. Running
    them against, say, an ``upload``/``upload`` row would rewrite it to
    ``upload``/``code`` with a workspace external id: an unregistered
    ``(integration, type)`` that the pane can only render as a disabled "No
    handler is registered" row, holding an external id that no longer matches
    the path its own download URL is built from.
    """
    identity = _identity_row(db, artifact_id)
    if identity is None or identity["state"] != "active":
        raise RelocationError(f"artifact not found or inactive: {artifact_id}")
    owner_thread_id = _owner_thread(db, artifact_id)
    if owner_thread_id != actor_thread_id:
        from infra.config import WORKSPACE

        if not identity["path"] or not is_scope_conductor_for_shared_path(
            actor_thread_id,
            identity["path"],
            workspace=WORKSPACE,
            db=db,
        ):
            raise RelocationError(
                "only the owning thread or the scope conductor may relocate this artifact"
            )
    if identity["integration"] != "workspace":
        raise RelocationError(
            "only workspace artifacts can be relocated, not "
            f"{identity['integration']!r}"
        )
    return identity


def _validated_move(
    db, identity: dict, target_path: str, actor_thread_id: str,
) -> dict:
    """Return the fully validated source/target for this move, or raise.

    Everything that can refuse a move happens here, before any intent row
    exists: a present local source, a registrable target outside every other
    thread's tray, a free target path in an existing directory, the same
    filesystem, and a target identity nobody else already holds.
    """
    source = identity["path"]
    if not source or "://" in source:
        raise RelocationError("artifact has no local backing file to relocate")
    source_p = Path(source)
    if not source_p.exists():
        raise RelocationError(f"source file does not exist: {source}")

    normalized = service.normalize_file_path(target_path, db=db)
    if normalized is None:
        raise RelocationError(
            f"target is outside the registrable roots: {target_path}"
        )
    target_canonical, target_zone, _default = normalized
    _reject_foreign_thread_tray(target_canonical, actor_thread_id)
    target_p = Path(target_canonical)
    if target_p.exists():
        raise RelocationError(f"target already exists: {target_canonical}")
    if not target_p.parent.is_dir():
        raise RelocationError(f"target directory does not exist: {target_p.parent}")

    try:
        same_device = source_p.stat().st_dev == target_p.parent.stat().st_dev
    except OSError as exc:
        raise RelocationError(f"could not verify target filesystem: {exc}") from exc
    if not same_device:
        raise RelocationError("cross-device relocation is not supported")

    target_type, target_title = target_identity(identity, source_p, target_p)
    target_external_id = external_identity_for_path(
        target_canonical, target_zone, db=db,
    )
    # ``find_identity_by_external_ids`` deliberately includes archived and
    # ownerless rows, so this also catches a collision with a retired identity
    # that still holds the UNIQUE(integration, type, external_id).
    existing_target = models.find_identity_by_external_ids(
        identity["integration"], target_type, (target_external_id,), db=db,
    )
    if existing_target is not None and existing_target["id"] != identity["id"]:
        raise RelocationError(
            f"target identity already registered: {target_external_id}"
        )

    return {
        "source_path": source,
        "target_path": target_canonical,
        "target_external_id": target_external_id,
        "target_type": target_type,
        "target_title": target_title,
    }


def relocate_artifact(
    artifact_id: str, target_path: str, *, actor_thread_id: str, db=None,
) -> dict:
    """Move one artifact's backing file, preserving its identity.

    Workspace artifacts and same-filesystem targets only; a non-workspace
    integration or a cross-device target is rejected loud. Its owner or scope
    conductor may relocate it. A supplied connection must be clean: no outer
    transaction may span the filesystem move.
    """
    _db = db if db is not None else get_db()
    if _db.in_transaction:
        raise TransactionOwnershipError(
            "artifact relocation requires a clean connection before file work"
        )
    try:
        identity = _authorized_identity(_db, artifact_id, actor_thread_id)
        move = _validated_move(_db, identity, target_path, actor_thread_id)

        now = _now()
        intent_id = str(uuid.uuid4())[:12]
        with write_transaction(_db, scope="relocation_intent") as connection:
            connection.execute(
                "INSERT INTO artifact_relocation_intents "
                "(id, artifact_id, actor_thread_id, source_path, target_path, "
                " source_external_id, target_external_id, target_type, target_title, "
                " phase, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'recorded',?,?)",
                (
                    intent_id, artifact_id, actor_thread_id,
                    move["source_path"], move["target_path"],
                    identity["external_id"], move["target_external_id"],
                    move["target_type"], move["target_title"], now, now,
                ),
            )

        _perform_relocation(_db, intent_id)
        artifact = models.get_artifact(artifact_id, db=_db)
    finally:
        if db is None:
            _db.close()
    if artifact is not None:
        service.notify_changed(artifact)
    return artifact


def write_existing_move(
    identity: dict,
    move: dict,
    *,
    actor_thread_id: str,
    db,
) -> dict:
    """Write one already-completed filesystem move through the relocation audit."""
    artifact_id = str(identity["id"])
    owner = _owner_thread(db, artifact_id)
    now = _now()
    with write_transaction(db, scope="adopt_moved_file_identity") as transaction:
        transaction.execute(
            "UPDATE artifact_identities SET path=?,external_id=?,type=?,title=?,"
            "updated_at=? WHERE id=?",
            (
                move["target_path"], move["target_external_id"],
                move["target_type"], move["target_title"], now, artifact_id,
            ),
        )
        _append_relocation_event(
            transaction,
            artifact_id=artifact_id,
            actor_thread_id=actor_thread_id,
            affected_thread_id=owner,
            reason="moved_outside_tools",
            source_path=str(identity["path"]),
            target_path=move["target_path"],
            source_external_id=identity["external_id"],
            target_external_id=move["target_external_id"],
            target_type=move["target_type"],
            causal_event_id=f"moved-outside-tools:{artifact_id}:{now}",
        )
    return models.get_artifact(artifact_id, db=db)


def _recorded_target_identity(db, intent: dict) -> tuple[str, str | None]:
    """Return the target ``(type, title)`` this intent must apply.

    Normally the writer recorded both when it validated the request, so
    recovery lands exactly the same end state. An intent recorded before those
    columns existed derives them now from the same helper the writer uses.
    """
    if intent.get("target_type") is not None:
        return intent["target_type"], intent.get("target_title")
    identity = _identity_row(db, intent["artifact_id"]) or {}
    return target_identity(
        identity, Path(intent["source_path"]), Path(intent["target_path"]),
    )


def _perform_relocation(db, intent_id: str) -> None:
    """Advance one relocation intent to completion from whatever phase it is
    in. Every step is individually idempotent, so this is safe both for a
    fresh relocation and for recovering an interrupted one."""
    row = db.execute(
        "SELECT * FROM artifact_relocation_intents WHERE id=?", (intent_id,)
    ).fetchone()
    if row is None:
        return
    intent = dict(row)

    if intent["phase"] == "recorded":
        source_exists = os.path.exists(intent["source_path"])
        target_exists = os.path.exists(intent["target_path"])
        if source_exists and not target_exists:
            os.rename(intent["source_path"], intent["target_path"])
        elif source_exists and target_exists:
            raise RuntimeError(
                f"relocation intent {intent_id} has both source and target "
                "present; refusing to guess which is authoritative"
            )
        elif not source_exists and not target_exists:
            raise RuntimeError(
                f"relocation intent {intent_id} lost both source and target"
            )
        # else: target only — the rename already happened before a prior
        # crash; fall through and just advance the phase.
        with write_transaction(db, scope="relocation_file_moved") as connection:
            connection.execute(
                "UPDATE artifact_relocation_intents SET phase='file_moved', "
                "updated_at=? WHERE id=?",
                (_now(), intent_id),
            )
        intent["phase"] = "file_moved"

    if intent["phase"] == "file_moved":
        target_type, target_title = _recorded_target_identity(db, intent)
        # Path, external key, type and title move together: a partial update
        # here is exactly what forks one artifact into two identities.
        with write_transaction(db, scope="relocation_identity") as connection:
            connection.execute(
                "UPDATE artifact_identities "
                "SET path=?, external_id=?, type=?, title=?, updated_at=? WHERE id=?",
                (
                    intent["target_path"], intent["target_external_id"],
                    target_type, target_title, _now(), intent["artifact_id"],
                ),
            )
            connection.execute(
                "UPDATE artifact_relocation_intents SET phase='identity_updated', "
                "updated_at=? WHERE id=?",
                (_now(), intent_id),
            )
        intent["target_type"] = target_type
        intent["phase"] = "identity_updated"

    if intent["phase"] == "identity_updated":
        _finalize_relocation(db, intent_id, intent)


def _append_relocation_event(
    db,
    *,
    artifact_id: str,
    actor_thread_id: str,
    affected_thread_id: str | None,
    reason: str,
    source_path: str,
    target_path: str,
    source_external_id: str,
    target_external_id: str,
    target_type: str,
    causal_event_id: str,
) -> None:
    """Append the one audited event shape for all identity-preserving moves."""
    _append_registration_event(
        db,
        artifact_id=artifact_id,
        event_type="relocated",
        actor_thread_id=actor_thread_id,
        authority=None,
        reason=reason,
        source_path=source_path,
        affected_thread_id=affected_thread_id,
        old_owner_thread_id=None,
        new_owner_thread_id=None,
        old_relationship_state=None,
        new_relationship_state=None,
        metadata={
            "source_path": source_path,
            "target_path": target_path,
            "source_external_id": source_external_id,
            "target_external_id": target_external_id,
            "target_type": target_type,
        },
        now=_now(),
        causal_event_id=causal_event_id,
    )


def _finalize_relocation(db, intent_id: str, intent: dict) -> None:
    """Commit the relocation audit and removal of its completed intent together."""
    with write_transaction(db, scope="relocation_finalize") as connection:
        _append_relocation_event(
            connection,
            artifact_id=intent["artifact_id"],
            actor_thread_id=intent["actor_thread_id"],
            affected_thread_id=intent["actor_thread_id"],
            reason="artifact_relocate",
            source_path=intent["source_path"],
            target_path=intent["target_path"],
            source_external_id=intent["source_external_id"],
            target_external_id=intent["target_external_id"],
            target_type=intent["target_type"],
            causal_event_id=f"relocation:{intent_id}",
        )
        connection.execute("DELETE FROM artifact_relocation_intents WHERE id=?", (intent_id,))


def recover_interrupted_relocations(*, db=None) -> list[str]:
    """Forward-complete pending relocations through the recovery owner."""
    from registry.relocation_recovery import recover_interrupted_relocations as recover

    return recover(db=db)
