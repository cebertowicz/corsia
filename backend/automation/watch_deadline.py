"""Give an armed watch that no deadline ends a deadline, keeping the watch.

Split from ``automation.watches`` (at its size cap), which still owns every
other watch transition. Only the schema step that gives existing event waits a
deadline calls this (``plans.event_wait.give_deadline``): a new event wait is
armed with its deadline from the start.

The watch keeps its id, artifact and expected event, so an agent holding the
id can still resolve it and the event still ends it. Only an armed watch whose
condition is not already a deadline changes, so a rerun changes nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone

from automation.watches import Watch, _row_to_watch
from registry.attention import validate_defer_condition


def set_deadline(db, watch_id: str, deadline: str) -> Watch | None:
    """Return the watch with its new deadline, or None if nothing changed."""
    condition = validate_defer_condition({"type": "deadline", "value": deadline})
    now = datetime.now(timezone.utc).isoformat()
    changed = db.execute(
        """UPDATE expected_event_watches
           SET condition_type='deadline', condition_value=?, updated_at=?
           WHERE id=? AND status='armed' AND condition_type<>'deadline'""",
        (condition["value"], now, watch_id),
    ).rowcount
    if changed != 1:
        return None
    row = db.execute(
        "SELECT * FROM expected_event_watches WHERE id=?", (watch_id,),
    ).fetchone()
    return _row_to_watch(dict(row))
