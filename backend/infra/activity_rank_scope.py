"""Resolve which thread ranks an activity scope."""

from __future__ import annotations

from infra.conductor import WORKSPACE_SCOPE, get_scope_coordinator


def ranker_for(db, user_id: str, project_id: str | None) -> tuple[str, dict | None]:
    """Return the rank-store scope and its accessible coordinating thread, if any.

    A project without a conductor uses the workspace's order, filtered by the
    caller. Project access must be checked by the caller before this function.
    """
    scope = project_id or WORKSPACE_SCOPE
    thread_id = get_scope_coordinator(scope, db=db)
    if project_id and not thread_id:
        scope = WORKSPACE_SCOPE
        thread_id = get_scope_coordinator(scope, db=db)
    if not thread_id:
        return scope, None
    row = db.execute(
        "SELECT id, title FROM threads WHERE id=? AND user_id=? AND purged_at IS NULL "
        "AND archived_at IS NULL AND COALESCE(lifecycle_state,'active')!='archived'",
        (thread_id, user_id),
    ).fetchone()
    if row is None:
        return scope, None
    return scope, {"thread_id": row["id"], "label": row["title"] or "Untitled thread"}
