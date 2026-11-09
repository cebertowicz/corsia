"""Weekly workflow observations from scoped run records."""
from datetime import date, timedelta
from typing import Any


def weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group by the full version identity, preserving unknown measurements."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        day = date.fromisoformat(row["started"])
        start = (day - timedelta(days=day.weekday())).isoformat()
        groups.setdefault((start, row["version_id"]), []).append(row)

    def average(items: list[dict[str, Any]], key: str) -> float | None:
        values = [item[key] for item in items if item[key] is not None]
        return sum(values) / len(values) if values else None

    result = []
    for (start, version), items in sorted(groups.items()):
        ended = [item for item in items if item["ended"]]
        result.append({"start": start, "version": items[0]["version"], "version_id": version, "runs": len(items),
                       "failed": sum(item["failed_step"] is not None for item in ended),
                       "first_try": average(items, "first_try"),
                       "corrections": average(items, "corrections"), "gate_rounds": average(items, "gate_rounds"),
                       "cost": average(items, "cost"), "minutes": average(items, "minutes"),
                       "owner_measure": average(items, "owner_measure")})
    return result
