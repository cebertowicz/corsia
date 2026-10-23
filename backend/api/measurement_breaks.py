"""Data-backed measurement starts and comparability breaks."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.measurement_scope import Grain
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.measurement_starts import (
    SOURCE_START_SQL as _SOURCE_START_SQL,
    source_group as _source_group,
    source_starts_query as _source_starts_query,
)


def _period(grain: Grain, value: str | None) -> str | None:
    """Return the viewer-local bucket containing one stored UTC timestamp."""
    if not value:
        return None
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return grain.period_of(instant.astimezone(ZoneInfo(grain.zone)).date().isoformat())


def _breaks_query(groups: Iterable[str]) -> str:
    """Read any as-yet-unretained starts plus persisted break metadata."""
    columns = [f"({_SOURCE_START_SQL[group]}) AS {group}" for group in groups]
    columns.append("""(
        SELECT COALESCE(json_group_array(json_object('period',period,'kind',kind,'reason',reason,'metric_group',metric_group)), '[]')
        FROM (SELECT period,kind,reason,metric_group FROM measure_breaks
              WHERE kind NOT IN ('start','model') ORDER BY period,kind,metric_group)
    ) AS worlds""")
    # Model boundaries are retained at rating-write time, so this request reads
    # only the small break table rather than grouping the rating history.
    return "SELECT " + ", ".join(columns)


def _stored_starts(db, groups: Iterable[str], *, kind: str = "start") -> dict[str, str | None]:
    """Read retained boundaries of one kind for the requested source groups."""
    requested = tuple(dict.fromkeys(groups))
    if not requested:
        return {}
    placeholders = ",".join("?" for _ in requested)
    rows = db.execute(
        f"SELECT metric_group,period FROM measure_breaks WHERE kind=? "
        f"AND metric_group IN ({placeholders})",
        (kind, *requested),
    )
    found = {str(row["metric_group"]): str(row["period"]) for row in rows}
    return {group: found.get(group) for group in requested}


def record_source_starts(db, starts: dict[str, str | None]) -> None:
    """Retain newly observed source boundaries without replacing an earlier one."""
    rows = [
        (value, "source" if group == "asks_hold" else "start", group,
         "user-owned holds first recorded" if group == "asks_hold" else "instrument source first recorded")
        for group, value in starts.items() if value
    ]
    if not rows:
        return
    with write_transaction(db, scope="record_measurement_starts"):
        db.executemany(
            "INSERT OR IGNORE INTO measure_breaks(period,kind,metric_group,reason) VALUES (?,?,?,?)",
            rows,
        )


def persist_source_starts(starts: dict[str, str | None]) -> None:
    """Write starts computed by a read lane through the ordinary writable DB lane."""
    db = get_db()
    try:
        record_source_starts(db, starts)
    finally:
        db.close()


async def persist_pending_starts(starts: dict[str, str | None]) -> None:
    """Flush one read lane's retained-boundary discoveries through the write lane."""
    if any(starts.values()):
        await run_in_db(persist_source_starts, starts)


def _model_breaks(db, grain: Grain) -> list[dict[str, str]]:
    """Build rater-change marks from retained UTC settlement instants."""
    versions = list(db.execute(
        "SELECT identity,started_at FROM measurement_model_versions ORDER BY started_at,identity"
    ))
    breaks: list[dict[str, str]] = []
    for _, started_at in versions[1:]:
        if period := _period(grain, str(started_at)):
            breaks.append({
                "period": period,
                "kind": "model",
                "reason": "rater changed",
                "metric_group": "estimated_human_hours_per_active_hour",
            })
    return breaks


def _derive_missing_starts(db, groups: Iterable[str]) -> dict[str, str | None]:
    """Read only source groups not yet retained; call before the short write above."""
    requested = tuple(group for group in dict.fromkeys(groups) if group in _SOURCE_START_SQL)
    if not requested:
        return {}
    row = db.execute(_source_starts_query(requested)).fetchone()
    return {group: str(row[group]) if row and row[group] else None for group in requested}


def source_started_at(db, metric: str, *, pending_starts: dict[str, str | None] | None = None) -> str | None:
    """Return one install-wide retained source start for the KPI-only caller."""
    return source_started_ats(db, (metric,), pending_starts=pending_starts).get(_source_group(metric))


def source_started_period(db, metric: str, grain: Grain, *, pending_starts: dict[str, str | None] | None = None) -> str | None:
    """Return an install-wide source start in the viewer's requested period."""
    return _period(grain, source_started_at(db, metric, pending_starts=pending_starts))


def source_started_ats(db, metrics: Iterable[str], *, pending_starts: dict[str, str | None] | None = None) -> dict[str, str | None]:
    """Use retained starts; hand bulk-read discoveries to the ordinary write lane."""
    groups = tuple(dict.fromkeys(_source_group(metric) for metric in metrics))
    starts = _stored_starts(db, groups)
    missing = tuple(group for group in groups if group in _SOURCE_START_SQL and not starts[group])
    derived = _derive_missing_starts(db, missing)
    if pending_starts is None:
        record_source_starts(db, derived)
    else:
        pending_starts.update(derived)
    return {group: starts.get(group) or derived.get(group) for group in groups}


def _break_metadata(db, metrics: Iterable[str], grain: Grain, *,
                    pending_starts: dict[str, str | None] | None = None) -> tuple[dict[str, str | None], list[dict[str, str]], str | None]:
    groups = tuple(dict.fromkeys(_source_group(metric) for metric in metrics))
    stored = _stored_starts(db, groups)
    missing = tuple(group for group in groups if group in _SOURCE_START_SQL and not stored[group])
    asks_hold_stored = _stored_starts(db, ("asks_hold",), kind="source")["asks_hold"] if "asks_per_outcome" in metrics else None
    query_groups = [*missing]
    if "asks_per_outcome" in metrics and not asks_hold_stored:
        query_groups.append("asks_hold")
    row = db.execute(_breaks_query(query_groups)).fetchone()
    derived = {group: str(row[group]) if row and row[group] else None for group in missing}
    if "asks_hold" in query_groups:
        derived["asks_hold"] = str(row["asks_hold"]) if row and row["asks_hold"] else None
    if pending_starts is None:
        record_source_starts(db, derived)
    else:
        pending_starts.update(derived)
    starts = {group: stored.get(group) or derived.get(group) for group in groups}
    asks_hold = asks_hold_stored or derived.get("asks_hold")
    worlds = [{**item, "period": grain.period_of(str(item["period"]))}
              for item in json.loads(row["worlds"] or "[]")]

    return starts, [*worlds, *_model_breaks(db, grain)], asks_hold


def derive_breaks(db, metrics: Iterable[str], grain: Grain, *,
                  pending_starts: dict[str, str | None] | None = None) -> dict[str, list[dict[str, str]]]:
    """Return requested series' retained starts and comparable boundaries."""
    requested = tuple(metrics)
    group_starts, worlds, asks_hold_value = _break_metadata(db, requested, grain, pending_starts=pending_starts)
    starts = {metric: _period(grain, group_starts[_source_group(metric)]) for metric in requested}
    asks_hold = _period(grain, asks_hold_value)
    result: dict[str, list[dict[str, str]]] = {}
    for metric, start in starts.items():
        rows: list[dict[str, str]] = []
        if start:
            rows.append({"period": start, "kind": "start", "reason": "instrument source first recorded"})
        if metric == "asks_per_outcome" and asks_hold and (not start or asks_hold > start):
            rows.append({"period": asks_hold, "kind": "source", "reason": "user-owned holds first recorded"})
        group = _source_group(metric)
        rows.extend(
            {key: value for key, value in item.items() if key != "metric_group"}
            for item in worlds
            if (((item.get("metric_group") is None and metric not in {"outcomes", "fresh_tokens"})
                 or item.get("metric_group") == group)
                and (not start or item["period"] > start))
        )
        result[metric] = rows
    return result


def first_availability_period(breaks: Iterable[dict[str, str]]) -> str | None:
    """Return the first source that can observe a metric, not later completeness marks."""
    return next((item["period"] for item in breaks if item["kind"] in {"start", "source"}), None)


def latest_source_mark_period(db, group: str) -> str | None:
    """Return a group's latest completeness/source mark for callers that need it."""
    row = db.execute(
        "SELECT MAX(period) FROM measure_breaks WHERE kind='source' AND metric_group=?", (group,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def comparability_start(breaks: Iterable[dict[str, str]]) -> str | None:
    """Return the final definition boundary a period must reach to be comparable."""
    marks = [item["period"] for item in breaks if item["kind"] in {"world", "model"}]
    return max(marks, default=None)


def period_comparable(period: str, breaks: Iterable[dict[str, str]]) -> bool:
    """Use the one derived list for chart rows and Agents findings alike."""
    start = comparability_start(breaks)
    return start is None or period >= start
