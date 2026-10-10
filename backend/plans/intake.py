"""Shared authorization for actor-bound thread-plan operations.

The former plan-specific discussion and free-form todo intake paths were retired
with the top-level Plans UX. Decision authorization still reuses the thread-plan
scope check here so that one boundary owns disclosure and archive behavior.
"""

from __future__ import annotations

import plans.boundary as boundary

_PLAN_UNAVAILABLE = "plan is unavailable for intake"
_THREAD_UNAVAILABLE = "plan owner thread is unavailable"
_SCOPE_DENIED = "public scope plan intake is unavailable"


def _authorize_thread_plan(db, plan_id: str, actor: boundary.PlanActor):
    """Resolve one accessible live thread plan before any existence disclosure."""
    actor = boundary._revalidate_actor(db, actor)
    plan = boundary._authorize_plan_scope(db, plan_id, actor)
    if plan["owner_kind"] != "thread":
        raise PermissionError(_SCOPE_DENIED)
    if plan["state"] == "archived":
        raise PermissionError(_PLAN_UNAVAILABLE)
    live = db.execute(
        """SELECT id FROM threads
           WHERE id=? AND user_id=? AND archived_at IS NULL AND purged_at IS NULL
             AND COALESCE(lifecycle_state,'active') != 'archived'""",
        (plan["owner_id"], actor.user_id),
    ).fetchone()
    if live is None:
        raise PermissionError(_THREAD_UNAVAILABLE)
    return plan, actor
