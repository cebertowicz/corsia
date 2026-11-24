"""Authority and scope checks for artifact relocation."""

from core.workspace_layout import thread_tray_owners
from registry.identity_bridge import OWNER_REGISTRATION_STATES
from registry.shared_principals import UI_PRINCIPAL, is_scope_conductor_for_shared_path


class RelocationError(ValueError):
    """A relocation request failed validation or cannot be completed."""


def identity_row(db, artifact_id: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM artifact_identities WHERE id=?", (artifact_id,)
    ).fetchone()
    return dict(row) if row else None


def owner_thread(db, artifact_id: str) -> str | None:
    placeholders = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    row = db.execute(
        f"""SELECT parent_thread_id FROM artifact_registrations
            WHERE artifact_id=? AND relationship_state IN ({placeholders})
            ORDER BY CASE relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                     updated_at DESC LIMIT 1""",
        (artifact_id, *OWNER_REGISTRATION_STATES),
    ).fetchone()
    return row["parent_thread_id"] if row else None


def reject_foreign_thread_tray(target_canonical: str, actor_thread_id: str) -> None:
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


def authorized_identity(db, artifact_id: str, actor_thread_id: str) -> dict:
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
    identity = identity_row(db, artifact_id)
    if identity is None or identity["state"] != "active":
        raise RelocationError(f"artifact not found or inactive: {artifact_id}")
    owner_thread_id = owner_thread(db, artifact_id)
    if owner_thread_id == UI_PRINCIPAL and identity["type"] == "document":
        raise RelocationError("Only the person may move their document.")
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
