"""The moves a declared KPI reports when its latest settled value is worse than its review line.

Pure reads over an agent's KPI result (``api.measurement_agent_kpis._read``) and its journals; nothing
here writes or wakes. ``jobs.kpi_thresholds`` delivers what this builds.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from events.movement_report import Move, PrecedingChange

ALERTS = ("wake", "digest")
# How far before the evaluated period a change still counts as having come before the move.
LOOKBACK = timedelta(days=7)


@dataclass(frozen=True)
class KpiMove:
    move: Move
    wake: bool


def _instant(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _worse(value: float, review: float, better: str) -> bool:
    return value < review if better == "higher" else value > review


def normal_range(spec: Mapping[str, Any]) -> list[float | None]:
    """The review line to the KPI's best bound: 0 or 1 where the unit has one, else no upper limit."""
    review = spec["review"]
    if spec["better"] == "lower":
        return [0, review]
    return [review, 1 if spec.get("unit") == "percent" else None]


def settled_value(spec: Mapping[str, Any], period: Mapping[str, Any]) -> float | None:
    """The reading to judge: a count needs only its count, every other formula needs min_n met."""
    value = period.get("count") if spec["formula"] == "count" else period.get("value")
    return value if _number(value) else None


def latest_settled(
    periods: Sequence[Mapping[str, Any]], bounds: Sequence[datetime], settle_days: int, now: datetime,
) -> tuple[Mapping[str, Any], datetime, datetime] | None:
    """The newest completed period whose settle days have passed, with its start and end."""
    for index in range(len(periods) - 2, -1, -1):
        if now >= bounds[index + 1] + timedelta(days=settle_days):
            return periods[index], bounds[index], bounds[index + 1]
    return None


def preceding_changes(
    db: sqlite3.Connection, owner: str, item: Mapping[str, Any], start: datetime, end: datetime,
) -> list[PrecedingChange]:
    """Journaled changes from a week before the period to its end: a listing, never a cause."""
    since = start - LOOKBACK
    found: list[tuple[datetime, PrecedingChange]] = []

    def add(kind: str, ref: str, at: datetime) -> None:
        if since <= at < end:
            found.append((at, PrecedingChange(kind, ref, at.isoformat())))

    if item["workflow"]:
        for version in item["versions"]:
            if version["version"] not in ("", "legacy"):
                add("workflow", f"{item['workflow']}@{version['version']}", _instant(version["first_seen"]))
    for path, synced in db.execute(
            "SELECT file_path, MIN(synced_at) FROM measure_definitions WHERE owner_thread_id=? "
            "GROUP BY file_path, file_sha256", (owner,)):
        add("skill", path, _instant(synced))
    models = db.execute(
        "SELECT model, MIN(created_at) AS first FROM llm_usage WHERE thread_id=? GROUP BY model ORDER BY first",
        (owner,)).fetchall()
    for row in models[1:]:  # the first model an agent used is where it began, not a change
        add("model", row["model"], _instant(row["first"]))
    return [change for _, change in sorted(found, key=lambda pair: pair[0])]


def kpi_moves(
    db: sqlite3.Connection, owner: str, result: Mapping[str, Any], bounds: Sequence[datetime], now: datetime,
) -> list[KpiMove]:
    """One move per alerting KPI whose latest settled value is strictly worse than its review line."""
    moves = []
    for item in result["kpis"]:
        spec = item["spec"]
        review = spec.get("review")
        if spec.get("alert") not in ALERTS or not _number(review) or spec.get("better") not in ("higher", "lower"):
            continue
        settled = latest_settled(item["periods"], bounds, spec.get("settle_days", 0), now)
        if settled is None:
            continue
        period, start, end = settled
        value = settled_value(spec, period)
        if value is None or not _worse(value, review, spec["better"]):
            continue
        moves.append(KpiMove(Move(
            f"kpi:{owner}:{item['name']}@v{item['definition_version']}", owner, value,
            tuple(normal_range(spec)), tuple(preceding_changes(db, owner, item, start, end))),
            spec["alert"] == "wake"))
    return moves
