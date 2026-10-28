"""Shared owner attribution for observed plan corrections."""

from __future__ import annotations


def attribution(executor: str | None, db) -> tuple[str | None, str | None]:
    """Find the nearest non-temporary agent, retaining an unresolvable executor."""
    from infra.queries import thread_is_temporary

    current, first, seen = executor, None, set()
    while current is not None and current not in seen:
        seen.add(current)
        row = db.execute(
            "SELECT id,spawned_by,agent_enabled,project_id FROM threads WHERE id=?", (current,),
        ).fetchone()
        if row is None:
            break
        first = first or row
        if not thread_is_temporary(row):
            return str(row["id"]), row["project_id"]
        current = row["spawned_by"]
    return executor, None if first is None else first["project_id"]
