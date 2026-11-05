"""Name each report row's owner projects with one read for the page being returned.

Membership is the Measurement closure itself (``scope_cte``): a thread belongs to the
project of its top-level ancestor. Owners come from lesson files as well as the database,
so they are only known once the page is rendered; this read follows the render.
"""
from __future__ import annotations

from threading import Event
from typing import Any

from core.lesson_report_db import report_snapshot
from core.measurement_scope import scope_cte

NO_OWNER_REASON = "no owner recorded"
UNRESOLVED_REASON = "an owner thread is not in this user's thread tree"


def owner_projects(db, user_id: str, owner_ids: set[str]) -> dict[str, str | None]:
    """Project id (None when the thread's tree has no project) per resolvable owner thread.

    The same-user closure walks every thread the user owns; filtering by owner afterwards
    never widens it. An owner outside the closure is absent from the result.
    """
    if not owner_ids:
        return {}
    names = {f"owner{index}": owner for index, owner in enumerate(sorted(owner_ids))}
    marks = ", ".join(f":{name}" for name in names)
    rows = db.execute(
        scope_cte() + "SELECT scope_threads.id AS id, root.project_id AS project_id FROM scope_threads "
        f"JOIN threads AS root ON root.id = scope_threads.root_id WHERE scope_threads.id IN ({marks})",
        {"user_id": user_id, "scope_project": "all", "scope_agent": None, **names},
    ).fetchall()
    return {row["id"]: row["project_id"] for row in rows}


def _summaries(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in result.get("rows", []) if "owner_ids" in row]
    detail = result.get("detail")
    return rows + ([detail] if isinstance(detail, dict) and "owner_ids" in detail else [])


def _attach(row: dict[str, Any], projects: dict[str, str | None]) -> None:
    owners = row["owner_ids"]
    if not owners:
        row.update(project_ids=None, project_ids_reason=NO_OWNER_REASON)
    elif any(owner not in projects for owner in owners):
        row.update(project_ids=None, project_ids_reason=UNRESOLVED_REASON)
    else:
        row.update(project_ids=sorted({project for owner in owners if (project := projects[owner])}),
                   project_ids_reason=None)


def attach_project_ids(result: dict[str, Any], user_id: str,
                       cancellation_event: Event | None = None) -> dict[str, Any]:
    """Add ``project_ids`` to each summary on the page, in one short read snapshot."""
    summaries = _summaries(result)
    if not summaries:
        return result
    owners = {owner for row in summaries for owner in row["owner_ids"]}
    with report_snapshot(cancellation_event=cancellation_event) as db:
        projects = owner_projects(db, user_id, owners)
    for row in summaries:
        _attach(row, projects)
    return result
