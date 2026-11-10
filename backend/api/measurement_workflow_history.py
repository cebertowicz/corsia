"""Version and weekly workflow observations from scoped run records."""
from typing import Any
from zoneinfo import ZoneInfo

from core.measurement_scope import Grain
from core.workflow_verdict_types import WorkflowHistory
from core.workflow_verdicts import parse_ts
from tools.workflow_task_fields import version_short_hash

MEASURES = ("first_try", "corrections", "gate_rounds", "cost", "minutes", "owner_measure")


def measures(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    """Return each recorded numerator and denominator, leaving missing values unknown."""
    result = {}
    for key in MEASURES:
        values = [row[key] for row in rows if row[key] is not None]
        total = sum(values) if values else None
        result[key] = {"numerator": total, "n": len(values), "value": total / len(values) if total is not None else None}
    return result


def version_summaries(rows: list[dict[str, Any]], history: WorkflowHistory, zone: ZoneInfo) -> list[dict[str, Any]]:
    """Each scoped version's measures, labelled with the engine's first use."""
    versions = list(dict.fromkeys(row["version_id"] for row in rows))
    return [{"id": version, "label": version_short_hash(version),
             "started": parse_ts(history.first_use(version)).astimezone(zone).date().isoformat(),
             "runs": len(items := [row for row in rows if row["version_id"] == version]),
             "measures": measures(items)} for version in versions]


def weekly(rows: list[dict[str, Any]], grain: Grain) -> list[dict[str, Any]]:
    """Group using Measurement's local weekly calendar and full version identity."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((grain.period_of(row["started"]), row["version_id"]), []).append(row)
    result = []
    for (start, version), items in sorted(groups.items()):
        values = measures(items)
        finished = [item for item in items if item["finished"]]
        result.append({"start": start, "version": items[0]["version"], "version_id": version, "runs": len(items),
                       "failed": sum(item["failed_step"] is not None for item in finished),
                       "measures": values})
    return result
