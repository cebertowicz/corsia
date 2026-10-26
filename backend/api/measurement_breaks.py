"""Data-backed Measurement starts and comparability marks."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from api.measurement_size import counted_raters
from core.measurement_scope import Grain
from infra.measurement_starts import has_live_start, live_starts_query, source_group


def local_date(grain: Grain, value: str) -> str:
    """Return the viewer-local calendar date of one stored UTC timestamp."""
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(ZoneInfo(grain.zone)).date().isoformat()


def _period(grain: Grain, value: str | None) -> str | None:
    """Return the viewer-local bucket containing one stored UTC timestamp."""
    return grain.period_of(local_date(grain, value)) if value else None


COMPARABILITY_UNRESTRICTED = frozenset({"outcomes", "fresh_tokens"})
# Two index probes: whether two raters were ever recorded. With one, no rater can change,
# so the request path skips reading every rating in install history.
_RATINGS = """FROM outcome_events INDEXED BY idx_measurement_size_rater
    WHERE event_name='measurement.size_rating' AND retracted_at IS NULL
      AND json_extract(attributes, '$.rater') > ''"""
_TWO_RATERS_SQL = (
    f"SELECT (SELECT MIN(json_extract(attributes, '$.rater')) {_RATINGS})"
    f" < (SELECT MAX(json_extract(attributes, '$.rater')) {_RATINGS})"
)


def _breaks_query(groups: Iterable[str], with_marks: bool) -> str:
    """Reuse canonical live starts, with immutable migration marks when the caller needs them."""
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
    return f"SELECT {', '.join(item for item in (live, marks if with_marks else '') if item)}"


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


def _dominant_rater(raters: Counter[str], prior: str | None) -> str:
    """Choose the largest period population; a tie keeps the prior rater, else resolves lexically."""
    largest = max(raters.values())
    tied = {rater for rater, count in raters.items() if count == largest}
    return prior if prior in tied else min(tied)


def _model_marks_from_raters(grain: Grain, rows: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """Mark each period whose dominant rater differs from the previous rated period's."""
    by_period: dict[str, Counter[str]] = defaultdict(Counter)
    for started_at, rater in rows:
        if period := _period(grain, started_at):
            by_period[period][rater] += 1
    marks: list[dict[str, str]] = []
    prior: str | None = None
    for period in sorted(by_period):
        rater = _dominant_rater(by_period[period], prior)
        if prior is not None and rater != prior:
            marks.append({
                "period": period,
                "kind": "model",
                "reason": "judging model changed",
                "metric_group": "estimated_human_hours_per_active_hour",
            })
        prior = rater
    return marks


def _model_marks(db, grain: Grain) -> list[dict[str, str]]:
    """Derive rater changes from the ratings that count; the first dominant rater is the baseline."""
    if not db.execute(_TWO_RATERS_SQL).fetchone()[0]:
        return []
    return _model_marks_from_raters(grain, counted_raters(db))


def _metadata(
    db, metrics: Iterable[str], grain: Grain, *, with_marks: bool = True,
) -> tuple[dict[str, str | None], list[dict[str, str]]]:
    """The one read-only resolver for metric starts and, unless only starts are asked for, comparable marks."""
    requested = tuple(metrics)
    groups = tuple(dict.fromkeys(source_group(metric) for metric in requested))
    seeded = _seeded_starts(db, groups)
    live_groups = tuple(group for group in groups if not seeded[group] and has_live_start(group))
    row = db.execute(_breaks_query(live_groups, with_marks)).fetchone() if live_groups or with_marks else None
    live = {
        group: str(row[group]) if group in live_groups and row and row[group] else None
        for group in groups
    }
    starts = {group: _period(grain, seeded[group] or live[group]) for group in groups}
    marks = [
        {"period": grain.period_of(str(item["period"])), "kind": str(item["kind"]),
         "reason": str(item["reason"]), "metric_group": item["metric_group"]}
        for item in json.loads(str(row["marks"]) if with_marks and row and row["marks"] else "[]")
    ]
    if with_marks and "estimated_human_hours_per_active_hour" in requested:
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
    return _metadata(db, (metric,), grain, with_marks=False)[0][source_group(metric)]


def first_availability_period(breaks: Iterable[dict[str, str]]) -> str | None:
    """Return the first period whose primary instrument can observe this metric."""
    return next((item["period"] for item in breaks if item["kind"] == "start"), None)


def latest_source_mark_period(db, group: str) -> str | None:
    """Return a migration-seeded completeness mark when a caller needs it."""
    row = db.execute(
        "SELECT MAX(period) FROM measure_breaks WHERE kind='source' AND metric_group=?", (group,)
    ).fetchone()
    return str(row[0]) if row and row[0] else None


_DEFINITION_KINDS = frozenset({"world", "model"})


def comparability_start(breaks: Iterable[dict[str, str]]) -> str | None:
    """Return the final definition boundary a period must reach to be comparable."""
    return max((item["period"] for item in breaks if item["kind"] in _DEFINITION_KINDS), default=None)


def next_comparability_boundary(period: str, breaks: Iterable[dict[str, str]]) -> dict[str, str] | None:
    """Return the nearest later model/world boundary; none means the period is comparable."""
    marks = [item for item in breaks if item["kind"] in _DEFINITION_KINDS and item["period"] > period]
    return min(marks, key=lambda item: item["period"]) if marks else None


def period_comparable(period: str, breaks: Iterable[dict[str, str]]) -> bool:
    """Use the one derived rule for chart rows, size rows and Agents findings alike."""
    return next_comparability_boundary(period, breaks) is None
