"""DuckDB-backed analytics query helpers for product events."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from infra import analytics_db
from infra.analytics_stats import stats_for_values, ttest_summary

log = logging.getLogger(__name__)


_JSON_PATH_RE = re.compile(
    r"(\w+)\s*->>\s*'([^']+)'"
)


def _normalize_json_path(expr: str) -> str:
    """Avoid DuckDB 1.5.x ``->>`` + bind-param cast bugs."""
    return _JSON_PATH_RE.sub(r"json_extract_string(\1, '\2')", expr)


def _now_utc() -> datetime:
    """Return tz-aware UTC ``datetime`` for SQL ``WHERE ts >= ?`` bounds."""
    return datetime.now(timezone.utc)


def _cutoff_param(since: timedelta | None) -> datetime | None:
    """Convert a relative ``since`` window to an absolute UTC timestamp."""
    if since is None:
        return None
    return _now_utc() - since


def _filters_to_sql(filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """Translate a flat filters dict into a SQL fragment + bind values."""
    if not filters:
        return "", []
    columns = {"category", "action", "session_id", "project_id"}
    fragments: list[str] = []
    binds: list[Any] = []
    for key, val in filters.items():
        if key in columns:
            fragments.append(f"{key} = ?")
            binds.append(val)
        else:
            fragments.append(f"json_extract_string(properties, '{key}') = ?")
            binds.append(val if isinstance(val, str) else json.dumps(val))
    return " AND ".join(fragments), binds


def funnel(
    steps: list[str],
    since: timedelta | None = timedelta(days=30),
    filters: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Count distinct sessions reaching ordered ``category.action`` steps."""
    if not steps:
        return {}
    conn = analytics_db.get_db()
    where_extra, extra_binds = _filters_to_sql(filters)
    since_ts = _cutoff_param(since)

    result: dict[str, int] = {}
    counts_by_step = _funnel_per_step_min_ts(conn, steps, since_ts, where_extra, extra_binds)
    surviving: set[str] = set()
    prev_ts: dict[str, datetime] = {}
    for i, step in enumerate(steps):
        step_map = counts_by_step.get(step, {})
        if i == 0:
            surviving = set(step_map.keys())
            for sid in surviving:
                prev_ts[sid] = step_map[sid]
        else:
            new_surviving: set[str] = set()
            for sid in surviving:
                if sid in step_map and step_map[sid] >= prev_ts[sid]:
                    new_surviving.add(sid)
                    prev_ts[sid] = step_map[sid]
            surviving = new_surviving
        result[step] = len(surviving)
    return result


def _funnel_per_step_min_ts(
    conn: Any,
    steps: list[str],
    since_ts: datetime | None,
    where_extra: str,
    extra_binds: list[Any],
) -> dict[str, dict[str, datetime]]:
    """Return ``{step -> {session_id -> min_ts}}`` for each step."""
    out: dict[str, dict[str, datetime]] = {}
    for step in steps:
        try:
            category, action = step.split(".", 1)
        except ValueError:
            log.warning("malformed funnel step %r (expected 'category.action')", step)
            out[step] = {}
            continue
        where = ["category = ?", "action = ?", "session_id IS NOT NULL"]
        binds: list[Any] = [category, action]
        if since_ts is not None:
            where.append("ts >= ?")
            binds.append(since_ts)
        if where_extra:
            where.append(where_extra)
            binds.extend(extra_binds)
        sql = (
            "SELECT session_id, MIN(ts) "
            "FROM product_events "
            "WHERE " + " AND ".join(where) + " "
            "GROUP BY session_id"
        )
        rows = conn.execute(sql, binds).fetchall()
        out[step] = {sid: ts for sid, ts in rows}
    return out


def cohort_compare(
    group_by: str,
    metric: str,
    since: timedelta | None = timedelta(days=30),
    filters: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Group events by ``group_by`` and compute the metric's distribution."""
    conn = analytics_db.get_db()
    group_by = _normalize_json_path(group_by)
    metric = _normalize_json_path(metric)
    where_extra, extra_binds = _filters_to_sql(filters)
    since_ts = _cutoff_param(since)
    where = ["1=1"]
    binds: list[Any] = []
    if since_ts is not None:
        where.append("ts >= ?")
        binds.append(since_ts)
    if where_extra:
        where.append(where_extra)
        binds.extend(extra_binds)
    sql = (
        f"SELECT {group_by} AS gv, TRY_CAST({metric} AS DOUBLE) AS mv "
        "FROM product_events "
        f"WHERE {' AND '.join(where)}"
    )
    rows = conn.execute(sql, binds).fetchall()
    grouped: dict[str, list[float]] = {}
    for gv, mv in rows:
        if mv is None:
            continue
        key = str(gv) if gv is not None else "<null>"
        grouped.setdefault(key, []).append(float(mv))
    return {k: stats_for_values(v).as_dict() for k, v in grouped.items()}


def before_after(
    event_filter: dict[str, Any],
    cutoff_ts: datetime,
    metric: str,
) -> dict[str, Any]:
    """Compare a metric's distribution before and after a cutoff."""
    conn = analytics_db.get_db()
    metric = _normalize_json_path(metric)
    where_extra, extra_binds = _filters_to_sql(event_filter)

    def _sample(side: Literal["before", "after"]) -> list[float]:
        op = "<" if side == "before" else ">="
        where = [f"ts {op} ?"]
        binds: list[Any] = [cutoff_ts]
        if where_extra:
            where.append(where_extra)
            binds.extend(extra_binds)
        sql = (
            f"SELECT TRY_CAST({metric} AS DOUBLE) "
            "FROM product_events "
            f"WHERE {' AND '.join(where)}"
        )
        return [
            float(row[0]) for row in conn.execute(sql, binds).fetchall()
            if row[0] is not None
        ]

    before = _sample("before")
    after = _sample("after")
    return {
        "before": stats_for_values(before).as_dict(),
        "after": stats_for_values(after).as_dict(),
        "ttest": ttest_summary(after, before),
    }


def time_series(
    metric: str,
    bucket: Literal["hour", "day", "week"] = "day",
    since: timedelta | None = timedelta(days=30),
    filters: dict[str, Any] | None = None,
    agg: Literal["count", "sum", "avg", "p95"] = "count",
) -> list[tuple[datetime, float | None]]:
    """Return a sparse bucketed time series."""
    conn = analytics_db.get_db()
    metric = _normalize_json_path(metric)
    where_extra, extra_binds = _filters_to_sql(filters)
    since_ts = _cutoff_param(since)
    where = ["1=1"]
    binds: list[Any] = []
    if since_ts is not None:
        where.append("ts >= ?")
        binds.append(since_ts)
    if where_extra:
        where.append(where_extra)
        binds.extend(extra_binds)
    if agg == "count":
        agg_expr = "COUNT(*)"
    elif agg == "sum":
        agg_expr = f"SUM(TRY_CAST({metric} AS DOUBLE))"
    elif agg == "avg":
        agg_expr = f"AVG(TRY_CAST({metric} AS DOUBLE))"
    elif agg == "p95":
        agg_expr = f"QUANTILE_CONT(TRY_CAST({metric} AS DOUBLE), 0.95)"
    else:  # pragma: no cover — Literal type guards this
        raise ValueError(f"unsupported agg: {agg}")
    sql = (
        f"SELECT date_trunc('{bucket}', ts) AS b, {agg_expr} AS v "
        "FROM product_events "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY b ORDER BY b"
    )
    rows = conn.execute(sql, binds).fetchall()
    return [(b, (float(v) if v is not None else None)) for b, v in rows]


def distribution_compare(
    filter_a: dict[str, Any],
    filter_b: dict[str, Any],
    metric: str,
) -> dict[str, Any]:
    """Compare a metric's distribution between two arbitrary event filters."""
    conn = analytics_db.get_db()
    metric = _normalize_json_path(metric)

    def _sample(filt: dict[str, Any]) -> list[float]:
        where_frag, binds = _filters_to_sql(filt)
        clauses = ["1=1"]
        if where_frag:
            clauses.append(where_frag)
        sql = (
            f"SELECT TRY_CAST({metric} AS DOUBLE) "
            "FROM product_events "
            f"WHERE {' AND '.join(clauses)}"
        )
        return [
            float(row[0]) for row in conn.execute(sql, binds).fetchall()
            if row[0] is not None
        ]

    a = _sample(filter_a)
    b = _sample(filter_b)
    return {
        "a": stats_for_values(a).as_dict(),
        "b": stats_for_values(b).as_dict(),
        "ttest": ttest_summary(a, b),
    }


def top_n(
    group_by: str,
    metric: str,
    n: int = 10,
    since: timedelta | None = timedelta(days=30),
    filters: dict[str, Any] | None = None,
    agg: Literal["count", "sum", "avg"] = "count",
) -> list[tuple[str, float]]:
    """Return a descending leaderboard of grouped metric values."""
    n = max(1, n)
    conn = analytics_db.get_db()
    group_by = _normalize_json_path(group_by)
    metric = _normalize_json_path(metric)
    where_extra, extra_binds = _filters_to_sql(filters)
    since_ts = _cutoff_param(since)
    where = ["1=1"]
    binds: list[Any] = []
    if since_ts is not None:
        where.append("ts >= ?")
        binds.append(since_ts)
    if where_extra:
        where.append(where_extra)
        binds.extend(extra_binds)
    if agg == "count":
        agg_expr = "COUNT(*)"
    elif agg == "sum":
        agg_expr = f"SUM(TRY_CAST({metric} AS DOUBLE))"
    elif agg == "avg":
        agg_expr = f"AVG(TRY_CAST({metric} AS DOUBLE))"
    else:  # pragma: no cover — Literal type guards this
        raise ValueError(f"unsupported agg: {agg}")
    sql = (
        f"SELECT {group_by} AS gv, {agg_expr} AS v "
        "FROM product_events "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY gv "
        "ORDER BY v DESC NULLS LAST "
        f"LIMIT {n}"
    )
    rows = conn.execute(sql, binds).fetchall()
    return [
        (str(gv) if gv is not None else "<null>", float(v) if v is not None else 0.0)
        for gv, v in rows
    ]
