"""The project summaries beside the Agents table: outcomes per project in the latest window."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from api.measurement_queries import PREPARED_ROOT_OUTCOMES, PREPARED_ROOTS
from core.measurement_scope import Grain


def project_summaries(db, rows: list[dict[str, Any]], grain: Grain) -> dict[str, list[dict[str, Any]]]:
    """Return PL-4 project rows: active in the Trend range, needs-a-look first, then inactive."""
    flagged: dict[str, int] = defaultdict(int)
    for row in rows:
        if any(rule.get("flagged") for rule in row["flags"].values()):
            project_id = row["agent"]["project"]["id"]
            if project_id:
                flagged[project_id] += 1
    projects = db.execute(f"""
        SELECT p.id,p.name,COALESCE(SUM(CASE WHEN o.period BETWEEN :from AND :to THEN o.outcomes END),0) AS outcomes
        FROM projects p
        JOIN (SELECT DISTINCT t.project_id FROM {PREPARED_ROOTS} r JOIN threads t ON t.id=r.id) s ON s.project_id=p.id
        LEFT JOIN (SELECT t.project_id,o.period,o.outcomes FROM {PREPARED_ROOT_OUTCOMES} o
                   JOIN threads t ON t.id=o.root_id) o ON o.project_id=p.id
        GROUP BY p.id,p.name
    """, {"from": grain.latest.first, "to": grain.latest.last}).fetchall()
    active: list[dict[str, Any]] = [{"project": {"id": item["id"], "name": item["name"]}, "flagged_agents": flagged[item["id"]],
               "outcomes": int(item["outcomes"])} for item in projects if item["outcomes"]]
    active.sort(key=lambda item: (not item["flagged_agents"], -item["outcomes"],
                                  item["project"]["name"], item["project"]["id"]))
    inactive = sorted(({"id": item["id"], "name": item["name"]} for item in projects if not item["outcomes"]),
                      key=lambda item: (item["name"], item["id"]))
    return {"active": active, "inactive": inactive}
