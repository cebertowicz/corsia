"""Canonical identity facts for the current thread row."""

from __future__ import annotations

from typing import Any


def thread_agent_identity(
    thread_row: Any,
) -> tuple[bool | None, bool | None, str | None, str | None]:
    """Return persistent-agent and subagent identity facts from a thread row."""
    keys = thread_row.keys()
    if "agent_enabled" not in keys:
        return None, None, None, None
    is_agent = bool(thread_row["agent_enabled"])
    if not is_agent:
        is_subagent = bool(thread_row["spawned_by"]) if "spawned_by" in keys else None
        return False, is_subagent, None, None
    title = thread_row["title"] if "title" in keys else None
    responsibility = (
        thread_row["agent_responsibility"] if "agent_responsibility" in keys else None
    )
    return True, False, title or None, responsibility or None


def repoint_spawned_by(db: Any, thread_id: str, new_parent_id: str | None) -> None:
    """Name a different thread as this one's spawner, or none (a visible root).

    The one writer of ``threads.spawned_by`` after a thread is created. The
    caller owns whether the move is allowed (``plans.delegation_adoption``
    adopts; ``core.thread_family`` detaches a durable agent whose spawner is
    deleted); the plan hierarchy that reads the column is re-derived here.
    """
    from plans.public import rehome

    db.execute(
        "UPDATE threads SET spawned_by = ? WHERE id = ?", (new_parent_id, thread_id),
    )
    rehome(thread_id, db=db)


def root_visible_thread_ids(thread_ids: list[str], *, db: Any | None = None) -> dict[str, str | None]:
    """Resolve visible roots in batches without changing lineage semantics.

    Each chain still rejects missing rows, cycles, and a scope-changing
    ancestor. One lookup runs per lineage depth across all requested threads.
    """
    from infra.database import get_db

    ids = list(dict.fromkeys(thread_ids))
    roots = dict.fromkeys(ids)
    if not ids:
        return roots
    owns = db is None
    connection = db or get_db()
    try:
        pending = {thread_id: thread_id for thread_id in ids}
        scopes: dict[str, tuple[Any, Any] | None] = dict.fromkeys(ids)
        visited = {thread_id: {thread_id} for thread_id in ids}
        while pending:
            current_ids = list(dict.fromkeys(pending.values()))
            marks = ",".join("?" for _ in current_ids)
            rows = {
                str(row["id"]): row
                for row in connection.execute(
                    f"SELECT id,user_id,project_id,spawned_by FROM threads WHERE id IN ({marks})",
                    current_ids,
                ).fetchall()
            }
            next_pending: dict[str, str] = {}
            for thread_id, current_id in pending.items():
                row = rows.get(current_id)
                if row is None:
                    continue
                row_scope = (row["user_id"], row["project_id"])
                if scopes[thread_id] is None:
                    scopes[thread_id] = row_scope
                elif row_scope != scopes[thread_id]:
                    continue
                parent = row["spawned_by"]
                if parent is None:
                    roots[thread_id] = current_id
                elif parent not in visited[thread_id]:
                    visited[thread_id].add(parent)
                    next_pending[thread_id] = str(parent)
            pending = next_pending
        return roots
    finally:
        if owns:
            connection.close()


def root_visible_thread_id(thread_id: str, *, db: Any | None = None) -> str | None:
    """Return one thread's visible root through the canonical batch resolver."""
    return root_visible_thread_ids([thread_id], db=db)[thread_id]


def visible_root_or_none(thread_id: str, *, db: Any | None = None) -> str | None:
    """Resolve display lineage without allowing lookup failures to escape."""
    try:
        return root_visible_thread_id(thread_id, db=db)
    except Exception as exc:
        from loguru import logger

        logger.warning("visible-root lookup failed for {}: {}", thread_id, exc)
        return None
