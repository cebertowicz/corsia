"""Data-backed Measurement starts and comparability marks."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.measurement_scope import Grain
from infra.measurement_starts import MODEL_VERSION_ROWS_SQL, has_live_start, live_starts_query, source_group


def _period(grain: Grain, value: str | None) -> str | None:
    """Return the viewer-local bucket containing one stored UTC timestamp."""
    if not value:
        return None
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return grain.period_of(instant.astimezone(ZoneInfo(grain.zone)).date().isoformat())


COMPARABILITY_UNRESTRICTED = frozenset({"outcomes", "fresh_tokens"})


def _breaks_query(groups: Iterable[str]) -> str:
    """Reuse canonical live starts with immutable migration marks."""
    requested = tuple(dict.fromkeys(groups))
    live_groups = tuple(group for group in requested if has_live_start(group))
    live = live_starts_query(live_groups).removeprefix("SELECT ") if live_groups else ""
    marks = """(
        SELECT COALESCE(json_group_array(json_object(
            'period',period,'kind',kind,'reason',reason,'metric_group',metric_group
        )), '[]')
        FROM (
            SELECT period,kind,reason,metric_group FROM measure_breaks
            WHERE kind != 'start' ORDER BY period,kind,metric_group
        )
    ) AS marks"""
    return f"SELECT {', '.join(item for item in (live, marks) if item)}"


def _seeded_starts(db, groups: Iterable[str]) -> dict[str, str | None]:
    """Read immutable legacy floor instants once; reads never change them."""
    requested = tuple(dict.fromkeys(groups))
    if not requested:
        return {}
    placeholders = ",".join("?" for _ in requested)
    rows = db.execute(
        f"SELECT metric_group,period FROM measure_breaks WHERE kind='start' "
        f"AND metric_group IN ({placeholders})", requested,
    )
    found = {str(row["metric_group"]): str(row["period"]) for row in rows}
    return {group: found.get(group) for group in requested}


def _model_marks(db, grain: Grain) -> list[dict[str, str]]:
    """Derive rater changes from current facts; the first rater is the baseline."""
    marks: list[dict[str, str]] = []
    periods: set[str] = set()
    for _, started_at in db.execute(MODEL_VERSION_ROWS_SQL):
        if (period := _period(grain, str(started_at))) and period not in periods:
            periods.add(period)
            marks.append({
                "period": period,
                "kind": "model",
                "reason": "judging model changed",
                "metric_group": "estimated_human_hours_per_active_hour",
            })
    return marks


def _metadata(db, metrics: Iterable[str], grain: Grain) -> tuple[dict[str, str | None], list[dict[str, str]]]:
    """The one read-only resolver for metric starts and comparable marks."""
    requested = tuple(metrics)
    groups = tuple(dict.fromkeys(source_group(metric) for metric in requested))
    seeded = _seeded_starts(db, groups)
    live_groups = tuple(group for group in groups if not seeded[group] and has_live_start(group))
    row = db.execute(_breaks_query(live_groups)).fetchone()
    live = {
        group: str(row[group]) if group in live_groups and row and row[group] else None
        for group in groups
    }
    starts = {group: _period(grain, seeded[group] or live[group]) for group in groups}
    marks = [
        {"period": grain.period_of(str(item["period"])), "kind": str(item["kind"]),
         "reason": str(item["reason"]), "metric_group": item["metric_group"]}
        for item in json.loads(str(row["marks"]) if row and row["marks"] else "[]")
    ]
    if "estimated_human_hours_per_active_hour" in requested:
        marks.extend(_model_marks(db, grain))
    return starts, marks


def derive_breaks(db, metrics: Iterable[str], grain: Grain) -> dict[str, list[dict[str, str]]]:
    """Return each requested metric's deterministic source start and definition marks."""
    requested = tuple(metrics)
    starts, marks = _metadata(db, requested, grain)
    result: dict[str, list[dict[str, str]]] = {}
    for metric in requested:
        start = starts[source_group(metric)]
        rows: list[dict[str, str]] = []
        if start:
            rows.append({"period": start, "kind": "start", "reason": "first recorded on this computer"})
        rows.extend(
            {key: value for key, value in mark.items() if key != "metric_group"}
            for mark in marks
            if ((mark["metric_group"] is None and metric not in COMPARABILITY_UNRESTRICTED)
                or mark["metric_group"] == source_group(metric))
            and (not start or mark["period"] > start)
        )
        result[metric] = rows
    return result


def source_started_period(db, metric: str, grain: Grain) -> str | None:
    """Return a non-series caller's canonical start from the same resolver."""
    return _metadata(db, (metric,), grain)[0][source_group(metric)]


def first_availability_period(breaks: Iterable[dict[str, str]]) -> str | None:
    """Return the first period whose primary instrument can observe this metric."""
    return next((item["period"] for item in breaks if item["kind"] == "start"), None)


def latest_source_mark_period(db, group: str) -> str | None:
    """Return a migration-seeded completeness mark when a caller needs it."""
    row = db.execute(
        "SELECT MAX(period) FROM measure_breaks WHERE kind='source' AND metric_group=?", (group,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def comparability_start(breaks: Iterable[dict[str, str]]) -> str | None:
    """Return the final definition boundary a period must reach to be comparable."""
    marks = [item["period"] for item in breaks if item["kind"] in {"world", "model"}]
    return max(marks, default=None)


def comparability_break_kind(breaks: Iterable[dict[str, str]]) -> str | None:
    """Return the kind of the latest boundary that makes prior periods incomparable."""
    marks = [item for item in breaks if item["kind"] in {"world", "model"}]
    return max(marks, key=lambda item: item["period"])["kind"] if marks else None


def period_comparable(period: str, breaks: Iterable[dict[str, str]]) -> bool:
    """Use the one derived list for chart rows and Agents findings alike."""
    start = comparability_start(breaks)
    return start is None or period >= start
