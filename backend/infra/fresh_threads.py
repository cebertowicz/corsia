"""Fresh CORSIA thread creation helpers.

This is intentionally narrow: it creates new CORSIA session rows and shapes the
visibility payload for clients. It does not attach provider sessions, fork
history, split ranges, dispatch sends, or reconstruct transcripts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from core.project_context import resolve_project_paths
from infra.config import WORKSPACE
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.execution_class import provider_label
from infra.local_only import local_only_model_refusal
from infra.queries import insert_message
from infra.send_broadcast import broadcast_message_rows


_SESSION_EVENT_FIELDS = (
    "id",
    "user_id",
    "profile",
    "title",
    "model",
    "project_id",
    "parent_session_id",
    "spawned_by",
    "local_only",
    "local_only_inherited_from_thread_id",
    "reasoning_effort",
    "created_at",
    "updated_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _field(row: Any, key: str) -> Any:
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    if isinstance(row, dict):
        return row.get(key)
    return None


# The event shape is the wire contract and keeps the serialized name; the row it
# is built from is a database row and uses the canonical column.
_EVENT_FIELD_SOURCE = {"parent_session_id": "parent_thread_id"}


def session_payload(row: Any) -> dict:
    """Return the stable client-visible session shape for creation events."""
    return {
        key: _field(row, _EVENT_FIELD_SOURCE.get(key, key))
        for key in _SESSION_EVENT_FIELDS
    }



class SpawnVisibilityRejected(ValueError):
    """Raised when a spawned thread cannot be linked to its parent safely."""


def _ensure_project_exists(db, project_id: str | None) -> None:
    if not project_id:
        return
    if not db.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone():
        raise ValueError(f"Project not found: {project_id}")


def create_fresh_thread(
    *,
    user_id: str,
    thread_id: str | None = None,
    profile: str | None = None,
    title: str = "New Chat",
    model: str | None = None,
    project_id: str | None = None,
    spawned_by: str | None = None,
    parent_thread_id: str | None = None,
    created_at: str | None = None,
    reasoning_effort: str | None = None,
    account_id: str | None = None,
    actor_thread_id: str | None = None,
    db=None,
) -> dict:
    """Insert a new sessions row and return its visibility payload.

    Callers pass only creation-time facts they already own. The selected
    ``model`` stays the routing truth for provider dispatch. ``spawned_by`` is
    the single source of truth for subagent-ness: set it (to the parent's id)
    for a spawned worker; leave it ``None`` for a top-level thread.
    """
    sid = thread_id or str(uuid.uuid4())[:8]
    now = created_at or _now()
    with write_transaction(db, scope="create_fresh_thread") as _db:
        _ensure_project_exists(_db, project_id)
        inherited_from = spawned_by or parent_thread_id or actor_thread_id
        parent = (
            _db.execute(
                "SELECT local_only FROM threads WHERE id = ?", (inherited_from,)
            ).fetchone()
            if inherited_from
            else None
        )
        local_only = bool(parent and parent["local_only"])
        refusal = local_only_model_refusal(
            model,
            thread_local_only=local_only,
            project_id=project_id,
            db=_db,
        )
        if refusal:
            raise ValueError(refusal)
        row = {
            "id": sid,
            "user_id": user_id,
            "profile": profile,
            "title": title,
            "model": model,
            "project_id": project_id,
            "parent_thread_id": parent_thread_id,
            "spawned_by": spawned_by,
            "local_only": int(local_only),
            "local_only_inherited_from_thread_id": inherited_from if local_only else None,
            "reasoning_effort": reasoning_effort,
            "created_at": now,
            "updated_at": now,
        }
        values = {key: value for key, value in row.items() if value is not None}
        cols = list(values.keys())
        marks = ", ".join("?" for _ in cols)
        _db.execute(
            f"INSERT INTO threads ({', '.join(cols)}, last_context_change_seq) "
            f"VALUES ({marks}, "
            "(SELECT COALESCE(MAX(seq), 0) FROM context_change_journal))",
            tuple(values[col] for col in cols),
        )
        from connections.account_selection import bind_model_account

        bind_model_account(sid, model, account_id=account_id,
                           actor_thread_id=actor_thread_id or spawned_by, db=_db)
    return session_payload(row)


def emit_session_created(session: dict) -> None:
    """Publish the fresh-thread visibility contract for WebSocket clients."""
    from infra import state

    try:
        state.broadcast_to_user(
            session["user_id"],
            {"type": "session_created", "session": session_payload(session)},
        )
    except Exception as exc:
        logger.warning(f"fresh-thread visibility broadcast failed: {exc}")


async def create_visible_fresh_thread(
    *,
    user_id: str,
    thread_id: str | None = None,
    profile: str | None = None,
    title: str = "New Chat",
    model: str | None = None,
    project_id: str | None = None,
    spawned_by: str | None = None,
    parent_thread_id: str | None = None,
    created_at: str | None = None,
    reasoning_effort: str | None = None,
    thread_kind: str | None = None,
    emit_spawn_chip: bool = False,
    account_id: str | None = None,
    actor_thread_id: str | None = None,
) -> dict:
    """Create a fresh visible thread and publish its visibility side effects.

    This is the shared fresh-create/spawn primitive. It preserves the selected
    ``model`` as dispatch truth and only writes the existing ``spawned_by`` +
    ``subagent_ref`` linkage when a caller is actually creating a spawned
    worker.
    """

    def _create() -> dict:
        with write_transaction(None, scope="create_visible_fresh_thread") as db:
            if spawned_by and not db.execute(
                "SELECT id FROM threads WHERE id = ? AND user_id = ?",
                (spawned_by, user_id),
            ).fetchone():
                raise SpawnVisibilityRejected(
                    "spawned_by: thread not found or not owned by the authenticated user"
                )
            return create_fresh_thread(
                user_id=user_id,
                thread_id=thread_id,
                profile=profile,
                title=title,
                model=model,
                project_id=project_id,
                spawned_by=spawned_by,
                parent_thread_id=parent_thread_id,
                created_at=created_at,
                reasoning_effort=reasoning_effort,
                account_id=account_id,
                actor_thread_id=actor_thread_id,
                db=db,
            )

    session = await run_in_db(_create)
    if spawned_by:
        from host_access.supervisor import register_host_access_descendant

        register_host_access_descendant(
            parent_thread_id=spawned_by,
            thread_id=session["id"],
            user_id=user_id,
        )
    if spawned_by and emit_spawn_chip:
        await emit_spawn_chip_marker(
            spawned_by,
            session["id"],
            title,
            model=model,
            thread_kind=thread_kind,
            provider=provider_label(model),
        )
    emit_session_created(session)
    return session


async def emit_spawn_chip_marker(
    spawner_id: str,
    worker_id: str,
    title: str,
    *,
    model: str | None = None,
    thread_kind: str | None = None,
    provider: str = "delegation",
) -> str | None:
    """Insert and broadcast the existing ``subagent_ref`` spawn chip shape.

    Best-effort after the row exists: a chip failure should not undo thread
    creation, matching the delegation route's historical behavior.
    """
    marker_meta = {
        "type": "subagent_ref",
        "child_thread_id": worker_id,
        "name": title,
        "provider": provider,
        "thread_kind": thread_kind,
        "model": model,
    }
    marker_text = f"Spawned: {title}"
    now = _now()

    try:
        def _insert_marker() -> str | None:
            with write_transaction(None, scope="insert_spawn_chip") as db:
                if not db.execute(
                    "SELECT id FROM threads WHERE id = ?", (spawner_id,)
                ).fetchone():
                    return None
                marker_id = str(uuid.uuid4())
                insert_message(
                    spawner_id,
                    "system",
                    marker_text,
                    marker_meta,
                    msg_id=marker_id,
                    db=db,
                    created_at=now,
                )
                return marker_id

        marker_id = await run_in_db(_insert_marker)
        if marker_id is None:
            return None
        await broadcast_message_rows(spawner_id, (marker_id,))
        return marker_id
    except Exception as exc:
        logger.warning("fresh thread spawn chip insert failed for {}: {}", worker_id, exc)
        return None


def resolve_spawn_working_dir(parent_thread_id: str, db=None) -> tuple[str, str | None]:
    """Resolve a fresh spawn's cwd from the parent thread's project scope."""
    _db = db or get_db()
    try:
        row = _db.execute(
            "SELECT project_id FROM threads WHERE id = ?", (parent_thread_id,)
        ).fetchone()
        project_id = row["project_id"] if row else None
        if project_id:
            _name, planning_root = resolve_project_paths(
                WORKSPACE,
                project_id,
                db=_db,
            )
            if planning_root is not None:
                return str(planning_root), project_id
        return str(WORKSPACE), project_id
    finally:
        if not db:
            _db.close()
