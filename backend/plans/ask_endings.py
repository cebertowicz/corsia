"""The one durable terminal-state writer for user artifact asks."""
from __future__ import annotations

#: Why an ask ended, mapped to the ``ended`` value its clearance records.
_ENDED_BY_WHY = {
    "withdrawn": "withdrawn", "task_ended": "closed", "replaced": "replaced",
    "retired": "retired", "rejected": "rejected",
    "revision_requested": "revision_requested",
    "answered_internally": "answered_internally",
}
ASK_ENDINGS = frozenset(_ENDED_BY_WHY.values())


def detail(*, why: str, actor_id: str | None = None) -> dict:
    """Return the terminal detail for one ask-clear path.

    The caller supplies who acted and the semantic path, not a precomputed
    state, so durable ask endings have one owner across direct resolve, task
    retirement, replacement, person dispositions, and startup migration. A
    direct ``resolved`` clear is the agent withdrawing its ask unless the user
    resolved it; a user's own answer records no ending, only that the user
    answered, so no reader falls back to the artifact's attention metadata.
    """
    if why == "resolved":
        from plans.boundary import is_user_audit_id

        why = "resolved_by_user" if is_user_audit_id(actor_id) else "withdrawn"
    if why == "resolved_by_user":
        return {"answered_by": "user"}
    return {"ended": _ENDED_BY_WHY[why]}
