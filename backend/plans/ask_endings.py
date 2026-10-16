"""The one durable terminal-state writer for user artifact asks."""
from __future__ import annotations

ASK_ENDINGS = frozenset({
    "withdrawn", "closed", "replaced", "retired", "rejected",
    "revision_requested", "answered_internally",
})


def detail(*, why: str, actor_id: str | None = None) -> dict | None:
    """Return the terminal detail for one ask-clear path, or no terminal detail.

    The caller supplies the semantic path, not a precomputed state, so durable
    ask endings have one owner across direct withdraw, task retirement,
    replacement, person dispositions, and startup migration.
    """
    if why == "resolved_by_user":
        return None
    if why == "withdrawn":
        return {"ended": "withdrawn"}
    endings = {
        "task_ended": "closed", "replaced": "replaced", "retired": "retired",
        "rejected": "rejected", "revision_requested": "revision_requested",
        "answered_internally": "answered_internally",
    }
    return {"ended": endings[why]}
