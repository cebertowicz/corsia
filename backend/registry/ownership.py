"""Explicit artifact ownership transfer and release operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from infra.database import get_db, write_transaction
from registry import node_retirement
from registry.identity_bridge import (
    OWNER_REGISTRATION_STATES,
    PARENT_CHAIN_REGISTRATION_STATE,
    ParentChainOwnershipError,
    _latest_released_owner_thread_id,
    _upsert_registration,
    settled_row_rebind,
)
from registry.removal_events import emit_owner_removal
from registry.registration_events import (
    _append_registration_event,
    _now,
    resolve,
    routing_registrations,
)


def _is_parent_chain_subscriber(db, artifact_id: str, thread_id: str) -> bool:
    return db.execute(
        "SELECT 1 FROM artifact_registrations "
        "WHERE artifact_id=? AND parent_thread_id=? AND relationship_state=?",
        (artifact_id, thread_id, PARENT_CHAIN_REGISTRATION_STATE),
    ).fetchone() is not None


def _identity(db, integration: str, type: str, external_id: str):
    return db.execute(
        "SELECT * FROM artifact_identities "
        "WHERE integration=? AND type=? AND external_id=?",
        (integration, type, external_id),
    ).fetchone()


def _owner(db, artifact_id: str):
    placeholders = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    return db.execute(
        f"""SELECT * FROM artifact_registrations
            WHERE artifact_id=? AND relationship_state IN ({placeholders})
            ORDER BY CASE relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                     updated_at DESC, id ASC LIMIT 1""",
        (artifact_id, *OWNER_REGISTRATION_STATES),
    ).fetchone()


def _clear_triage(db, artifact_id: str, now: str) -> None:
    db.execute(
        """UPDATE artifact_registrations
           SET relationship_state='subscriber-recent', updated_at=?
           WHERE artifact_id=? AND relationship_state='unassigned-triage'""",
        (now, artifact_id),
    )


def rehome_artifact(
    *,
    integration: str,
    type: str,
    external_id: str,
    new_owner_thread_id: str,
    actor_thread_id: str | None,
    authority: str,
    reason: str,
    source_path: str,
    causal_event_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    db=None,
    removal_event: dict[str, Any] | None = None,
    show_to_new_owner: bool = True,
) -> dict[str, Any]:
    """Move an artifact's owner; only a deliberate hand-off shows it in their pane."""
    with write_transaction(db, scope="rehome_artifact") as conn:
        identity = _identity(conn, integration, type, external_id)
        if identity is None:
            raise ValueError("artifact not found")
        artifact_id = identity["id"]
        if _is_parent_chain_subscriber(conn, artifact_id, new_owner_thread_id):
            raise ParentChainOwnershipError(
                f"thread {new_owner_thread_id} is a parent_chain subscriber of "
                f"{integration}/{type}/{external_id}; it cannot become owner"
            )

        owner = _owner(conn, artifact_id)
        old_owner = owner["parent_thread_id"] if owner else None
        old_state = owner["relationship_state"] if owner else None
        shown_before = node_retirement.pane_threads(conn, artifact_id)
        keep = new_owner_thread_id if show_to_new_owner else None
        now = _now()
        if old_owner and old_owner != new_owner_thread_id:
            _upsert_registration(
                conn,
                artifact_id=artifact_id,
                parent_thread_id=old_owner,
                relationship_state="subscriber-recent",
                now=now,
            )
        _clear_triage(conn, artifact_id, now)
        conn.execute(
            """UPDATE artifact_registrations
               SET relationship_state='subscriber-recent', updated_at=?
               WHERE artifact_id=? AND parent_thread_id<>?
                 AND relationship_state IN ('owner-pinned', 'owner-active')""",
            (now, artifact_id, new_owner_thread_id),
        )
        _upsert_registration(
            conn,
            artifact_id=artifact_id,
            parent_thread_id=new_owner_thread_id,
            relationship_state="owner-active",
            now=now,
            rebind=settled_row_rebind(conn, artifact_id, keep),
        )
        conn.execute(
            "UPDATE artifact_identities SET state='active', updated_at=? WHERE id=?",
            (now, artifact_id),
        )
        event_id = _append_registration_event(
            conn,
            artifact_id=artifact_id,
            event_type="ownership_rehomed",
            actor_thread_id=actor_thread_id,
            authority=authority,
            reason=reason,
            source_path=source_path,
            causal_event_id=causal_event_id,
            affected_thread_id=new_owner_thread_id,
            old_owner_thread_id=old_owner,
            new_owner_thread_id=new_owner_thread_id,
            old_relationship_state=old_state,
            new_relationship_state="owner-active",
            metadata=metadata,
            now=now,
        )
        node_retirement.retire_rows_ownership_added(
            conn, artifact_id, shown_before, keep=keep,
            trigger="ownership_rehomed", causal_event_id=causal_event_id,
        )
        chat_event = (
            emit_owner_removal(conn, identity, old_owner, "ownership_rehomed", removal_event)
            if old_owner and old_owner != new_owner_thread_id else None
        )
        result = {
            "event_id": event_id,
            "artifact": resolve(integration, type, external_id, db=conn),
            "old_owner_thread_id": old_owner,
            "new_owner_thread_id": new_owner_thread_id,
        }
        if chat_event is not None:
            result["removal_event"] = chat_event
        return result


def release_artifact(
    *,
    integration: str,
    type: str,
    external_id: str,
    actor_thread_id: str | None,
    authority: str,
    reason: str,
    source_path: str,
    triage_thread_id: str | None = None,
    causal_event_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    db=None,
    removal_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Release the current owner without assigning a replacement owner."""
    with write_transaction(db, scope="release_artifact") as conn:
        state = routing_registrations(integration, type, external_id, db=conn)
        identity = state.get("identity")
        owner = state.get("owner")
        if identity is None or owner is None:
            raise ValueError("artifact owner not found")

        artifact_id = identity["id"]
        old_owner = owner["parent_thread_id"]
        old_state = owner["relationship_state"]
        shown_before = node_retirement.pane_threads(conn, artifact_id)
        now = _now()
        conn.execute(
            """UPDATE artifact_registrations
               SET relationship_state='subscriber-recent', updated_at=?
               WHERE artifact_id=?
                 AND relationship_state IN ('owner-pinned', 'owner-active')""",
            (now, artifact_id),
        )
        _clear_triage(conn, artifact_id, now)
        if triage_thread_id:
            _upsert_registration(
                conn,
                artifact_id=artifact_id,
                parent_thread_id=triage_thread_id,
                relationship_state="unassigned-triage",
                now=now,
            )
        event_id = _append_registration_event(
            conn,
            artifact_id=artifact_id,
            event_type="ownership_released",
            actor_thread_id=actor_thread_id,
            authority=authority,
            reason=reason,
            source_path=source_path,
            causal_event_id=causal_event_id,
            affected_thread_id=triage_thread_id or old_owner,
            old_owner_thread_id=old_owner,
            new_owner_thread_id=None,
            old_relationship_state=old_state,
            new_relationship_state="subscriber-recent",
            metadata=metadata,
            now=now,
        )
        node_retirement.retire_rows_ownership_added(
            conn, artifact_id, shown_before, trigger="ownership_released",
            keep=None, causal_event_id=causal_event_id,
        )
        chat_event = emit_owner_removal(
            conn, identity, old_owner, "ownership_released", removal_event,
        )
        result = {
            "event_id": event_id,
            "artifact": resolve(integration, type, external_id, db=conn),
            "old_owner_thread_id": old_owner,
            "new_owner_thread_id": None,
            "triage_thread_id": triage_thread_id,
        }
        if chat_event is not None:
            result["removal_event"] = chat_event
        return result


def latest_released_owner_thread_id(artifact_id: str, *, db=None) -> str | None:
    """Return the released owner only when release is the latest disposition."""
    conn = db or get_db()
    try:
        return _latest_released_owner_thread_id(conn, artifact_id)
    finally:
        if not db:
            conn.close()


def _thread_value(thread, key: str, default=None):
    try:
        return thread[key]
    except (IndexError, KeyError):
        return default


def _inside_thread_folder(path: str | None, thread_id: str) -> bool:
    if not path:
        return False
    parts = Path(path).parts
    return any(
        part == ".threads" and index + 1 < len(parts) and parts[index + 1] == thread_id
        for index, part in enumerate(parts)
    )


def _shared_root(db, thread) -> str:
    from core.workspace_layout import resolve_project_root
    from infra.config import WORKSPACE

    project_id = _thread_value(thread, "project_id")
    if project_id is None:
        return str((WORKSPACE / "shared").resolve(strict=False))
    root = resolve_project_root(WORKSPACE, project_id, db=db)
    return str((root / "shared").resolve(strict=False)) if root is not None else ""


def _is_shared_document(path: str | None, shared_root: str) -> bool:
    if not path or not shared_root:
        return False
    candidate = Path(path).resolve(strict=False)
    root = Path(shared_root)
    return candidate != root and candidate.is_relative_to(root)


def _live_ancestor(db, thread, ending: set[str]) -> str | None:
    current = _thread_value(thread, "spawned_by")
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(str(current))
        row = db.execute(
            "SELECT id,spawned_by,lifecycle_state FROM threads WHERE id=?",
            (current,),
        ).fetchone()
        if row is None:
            return None
        state = row["lifecycle_state"] or "active"
        if str(row["id"]) not in ending and state not in ("archived", "purged"):
            return str(row["id"])
        current = row["spawned_by"]
    return None


def _live_conductor_or_user(db, project_id: str | None, ending: set[str]) -> str:
    """Return a usable conductor principal, falling back to the user."""
    from registry.shared_principals import UI_PRINCIPAL, scope_conductor

    conductor = scope_conductor(project_id, db=db)
    if not conductor or conductor in ending:
        return UI_PRINCIPAL
    row = db.execute(
        "SELECT lifecycle_state FROM threads WHERE id=?",
        (conductor,),
    ).fetchone()
    if row is None or (row["lifecycle_state"] or "active") in ("archived", "purged"):
        return UI_PRINCIPAL
    return conductor


def _thread_end_target(db, identity, thread, ending: set[str], *, reason: str) -> str | None:
    from registry.shared_principals import acting_owner

    if _is_shared_document(identity["path"], _shared_root(db, thread)):
        persistent = bool(_thread_value(thread, "agent_enabled")) or reason == "agent_disabled"
        project_id = _thread_value(thread, "project_id")
        fallback = _live_conductor_or_user(db, project_id, ending)
        if persistent:
            return fallback
        parent = _thread_value(thread, "spawned_by")
        target = (
            acting_owner(str(parent), False, db=db, excluded_thread_ids=ending)
            if parent else None
        )
        return target or fallback
    return _live_ancestor(db, thread, ending)


def _allow_parent_chain_owner(db, artifact_id: str, target: str) -> None:
    """Remove only the parent-chain row used by a thread-end owner transition."""
    db.execute(
        "DELETE FROM artifact_registrations WHERE artifact_id=? "
        "AND parent_thread_id=? AND relationship_state=?",
        (artifact_id, target, PARENT_CHAIN_REGISTRATION_STATE),
    )


def rehome_on_thread_end(
    db,
    thread_id: str,
    reason: str,
    *,
    ending_thread_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Move durable ownership before one thread becomes unavailable."""
    if reason not in {"archived", "purged", "deleted", "agent_disabled"}:
        raise ValueError(f"unsupported thread-end reason: {reason}")
    thread = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if thread is None:
        return []
    ending = set(ending_thread_ids or ())
    ending.add(thread_id)
    marks = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    # Only active identities carry ownership; rehoming an archived one revives it.
    rows = db.execute(
        f"""SELECT i.*,r.relationship_state FROM artifact_identities i
            JOIN artifact_registrations r ON r.artifact_id=i.id
            WHERE r.parent_thread_id=? AND r.relationship_state IN ({marks})
              AND i.state='active'
            ORDER BY i.id""",
        (thread_id, *OWNER_REGISTRATION_STATES),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for identity in rows:
        if identity["retention"] == "ephemeral":
            continue
        if _inside_thread_folder(identity["path"], thread_id):
            continue
        target = _thread_end_target(db, identity, thread, ending, reason=reason)
        common = {
            "integration": identity["integration"],
            "type": identity["type"],
            "external_id": identity["external_id"],
            "actor_thread_id": None,
            "authority": "thread_lifecycle",
            "reason": f"thread_ended:{reason}",
            "source_path": "registry.ownership.rehome_on_thread_end",
            "metadata": {"ended_thread_id": thread_id},
            "db": db,
        }
        if target:
            _allow_parent_chain_owner(db, identity["id"], target)
            results.append(rehome_artifact(
                new_owner_thread_id=target, show_to_new_owner=False, **common,
            ))
        elif reason != "deleted":
            # A delete drops the owner row; the next registrant claims, unreleased.
            results.append(release_artifact(**common))
    return results
