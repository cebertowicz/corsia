"""Who owns each presented plan: the owner label an operator read shows (split from ``operator_projection``)."""

from __future__ import annotations

import infra.config as config
from infra.packed_rows import fetch_all
import plans.hierarchy as hierarchy
import plans.rollup as rollup
from plans.boundary import PlanActor

# Rollups list only live threads; a direct read of one thread or worker also
# reaches an archived one, since archiving hides a thread but keeps its plan.
# Only a purged thread is gone.
LIVE_THREAD = "archived_at IS NULL AND COALESCE(lifecycle_state,'active') != 'archived'"


def _plan_row(item):
    """Accept a full rollup or its plan row for shared owner filtering."""
    return item.plan if isinstance(item, rollup.PlanRollup) else item


def owner_index(
    projected: list, actor: PlanActor, *, db, include_archived: bool,
    exclude_archived_projects: bool = False,
) -> dict:
    """Batch accessible owner labels for full or compact plan projections.

    ``exclude_archived_projects`` also drops a thread owner whose project is
    archived; the Activity rollups set it, the other readers keep their rows.
    """
    thread_ids = {
        _plan_row(item)["owner_id"] for item in projected
        if _plan_row(item)["owner_kind"] == "thread"
    }
    project_ids = {
        _plan_row(item)["owner_id"] for item in projected
        if _plan_row(item)["owner_kind"] == "project_scope"
    }
    sessions = {}
    live = "" if include_archived else f" AND {LIVE_THREAD}"
    if exclude_archived_projects:
        live += (
            " AND COALESCE(project_id,'') NOT IN"
            " (SELECT id FROM projects WHERE COALESCE(status,'active')='archived')"
        )
    ordered_threads = sorted(thread_ids)
    for start in range(0, len(ordered_threads), hierarchy._ID_CHUNK):
        chunk = ordered_threads[start:start + hierarchy._ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = fetch_all(
            db,
            f"""SELECT id, title, project_id FROM threads WHERE id IN ({marks})
                AND user_id=? AND purged_at IS NULL{live}""",
            (*chunk, actor.user_id),
        )
        sessions.update({row["id"]: row for row in rows})
    memberships = {
        row["project_id"] for row in fetch_all(
            db, "SELECT DISTINCT project_id FROM threads WHERE user_id=?", (actor.user_id,),
        )
    }
    projects = {}
    ordered_projects = sorted(project_ids)
    for start in range(0, len(ordered_projects), hierarchy._ID_CHUNK):
        chunk = ordered_projects[start:start + hierarchy._ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = fetch_all(
            db,
            f"""SELECT id, name FROM projects WHERE id IN ({marks})
                AND COALESCE(status,'active') != 'archived'""",
            tuple(chunk),
        )
        projects.update({row["id"]: row for row in rows if row["id"] in memberships})
    owners = {}
    for item in projected:
        plan = _plan_row(item)
        if plan["owner_kind"] == "workspace_scope":
            owner = {
                "label": config.WORKSPACE_SELECTION.name,
                "project_id": None,
                "thread_id": None,
            }
        elif plan["owner_kind"] == "project_scope" and plan["owner_id"] in projects:
            owner = {
                "label": projects[plan["owner_id"]]["name"],
                "project_id": plan["owner_id"],
                "thread_id": None,
            }
        elif plan["owner_kind"] == "thread" and plan["owner_id"] in sessions:
            session = sessions[plan["owner_id"]]
            owner = {
                "label": session["title"] or "Untitled thread",
                "project_id": session["project_id"],
                "thread_id": plan["owner_id"],
            }
        else:
            continue
        owners[plan["id"]] = owner
    return owners
