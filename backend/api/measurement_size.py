"""The estimated-size Measurement series: human-equivalent hours per active hour.

Reads the canonical accepted-outcome population for the request's scope and
periods, and for each outcome the newest size-rating fact by server observation
time. A retracted, malformed, stale, conflicting or explicitly unknown newest
fact contributes no hours and is counted, so an older favorable rating can never
resurface. The denominator is the scope's presence time, so the caller measures the
rate only from the first presence period; the outcome count is the same task count
the outcomes series serves, never a count of ratings.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Collection
from typing import Any

from api.measurement_queries import query_family
from core.size_rating import (
    AUTHOR_DECLARED_PROFILE, BASIS_AUTHOR_DECLARED, CLASS_MIDPOINT_HOURS, LATEST_EVIDENCE_SQL, NO_EVIDENCE,
    PROFILE_KEYS, SERVED_BASES, SIZE_EVENT, SUBJECT_PREFIX, UNKNOWN, UNKNOWN_VERSION,
    fact_status, profile,
)

STATUSES = ("usable", "unknown", "stale", "conflict", "retracted", "malformed")

def _nodes_sql(node: str, source: str, extra: str) -> str:
    """Each outcome node's period, plan revision, declared size and newest evidence, then one ``extra`` column."""
    return f"""
    SELECT {node} AS id,o.period,n.plan_id,n.revision,n.size,
      COALESCE(({LATEST_EVIDENCE_SQL.format(column='id', node=node)}),'{NO_EVIDENCE}') AS evidence_event_id,{extra}
    FROM {source} o JOIN plan_nodes n ON n.id={node}
"""


def _newest_facts_sql(nodes: str) -> str:
    """The newest size facts per node of ``nodes``, with every row tied at the newest observation time.

    Equal-time disagreement stays visible instead of being broken by an arbitrary id. This is
    the one rule that decides which rating counts; every reader of a size class uses it.
    """
    return f"""
    nodes AS ({nodes}), facts AS (
      SELECT nodes.id AS node_id,f.observed_ts,f.attributes,f.retracted_at,
        MAX(f.observed_ts) OVER (PARTITION BY nodes.id) AS newest
      FROM nodes JOIN outcome_events f ON f.subject_key='{SUBJECT_PREFIX}'||nodes.id AND f.event_name='{SIZE_EVENT}'
    ) SELECT node_id,attributes,retracted_at FROM facts WHERE observed_ts=newest
"""


# The nodes ``prepare_outcome_periods`` already materialized: every one with its first-try mark, or the
# requested periods' with their root.
_PREPARED_ALL_NODES_SQL = _nodes_sql("o.node_id", "measurement_outcome_nodes", "o.first_try")
_PREPARED_ALL_FACTS_SQL = "WITH " + _newest_facts_sql(_PREPARED_ALL_NODES_SQL)
_PREPARED_NODES_SQL = (_nodes_sql("o.node_id", "measurement_outcome_nodes", "o.root_id")
                       + " WHERE o.period IN (SELECT value FROM json_each(:periods))")
_PREPARED_FACTS_SQL = "WITH " + _newest_facts_sql(_PREPARED_NODES_SQL)


def _newest_status(facts: list[tuple[str, str | None]], current: tuple[str, int, str]) -> tuple[str, dict | None]:
    distinct = {(attributes, retracted is not None) for attributes, retracted in facts}
    if len(distinct) > 1:
        return "conflict", None
    attributes_text, retracted = next(iter(distinct))
    try:
        attributes = json.loads(attributes_text)
    except (TypeError, ValueError):
        attributes = None
    status = fact_status(attributes, retracted, current)
    return status, attributes if status == "usable" else None


def _node_size(
    node_size: str | None, facts: list[tuple[str, str | None]] | None, current: tuple[str, int, str],
) -> tuple[str, dict | None, str | None]:
    """One outcome's size status, usable rating attributes, and size class (None when it has none).

    The newest rating fact decides; with no fact, the size the task's author
    declared on the node counts, and an undeclared or unknown size counts no class.
    """
    if facts is None:
        if node_size is None:
            return "unsized", None, None
        if node_size == UNKNOWN:
            return "unknown", None, None
        return "usable", None, node_size
    status, attributes = _newest_status(facts, current)
    return status, attributes, attributes["size_class"] if attributes else None


def _classified(nodes: list, fact_rows: list) -> list[tuple[str, str, Any, str, dict | None, str | None]]:
    """Each node row: (id, period, its extra column, status, usable attributes, size class), by the newest facts."""
    facts: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for node_id, attributes, retracted in fact_rows:
        facts[node_id].append((attributes, retracted))
    return [
        (node_id, period, extra, *_node_size(node_size, facts.get(node_id), (plan_id, int(revision), evidence_event_id)))
        for node_id, period, plan_id, revision, node_size, evidence_event_id, extra in nodes
    ]


def _sized_nodes(db, params: dict[str, Any]) -> list[tuple[str, str, int, str, dict | None, str | None]]:
    """Each accepted outcome in the request: (id, period, first_try, status, attributes, size class).

    Reads the nodes ``prepare_outcome_periods`` settled on this connection for these same ``params``,
    so the scope is not settled again; call it only after that preparation.
    """
    rows = _classified(db.execute(_PREPARED_ALL_NODES_SQL).fetchall(), db.execute(_PREPARED_ALL_FACTS_SQL).fetchall())
    return [(node_id, period, int(first_try), status, attributes, size_class)
            for node_id, period, first_try, status, attributes, size_class in rows]


def outcome_sizes(db, params: dict[str, Any]) -> dict[str, tuple[str | None, int]]:
    """Each accepted outcome's size class (None when it has none) and first-try mark, for the request's periods.

    The class follows the same rule the estimated-size series counts hours by.
    """
    return {node_id: (size_class, first_try) for node_id, _, first_try, _, _, size_class in _sized_nodes(db, params)}


def rated_tasks(db, periods: Collection[str]) -> dict[str, tuple[str, str, float]]:
    """Each prepared accepted outcome in ``periods`` with a size class: ``{node_id: (root_id, period, midpoint hours)}``.

    Reads the settled nodes ``prepare_outcome_periods(roots=True)`` already materialized, so it does not settle
    the scope again; the newest fact and the size rule are the ones the estimated-size series counts by.
    """
    bound = {"periods": json.dumps(sorted(periods))}
    rows = _classified(db.execute(_PREPARED_NODES_SQL, bound).fetchall(), db.execute(_PREPARED_FACTS_SQL, bound).fetchall())
    return {node_id: (root_id, period, CLASS_MIDPOINT_HOURS[size_class])
            for node_id, period, root_id, _, _, size_class in rows if size_class is not None}


def size_family(db, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one aggregate per period with outcomes or active time in the request."""
    periods = size_period_totals(db, params)
    for active in query_family(db, "active", params):
        periods[active["period"]]["active_hours"] = active["active_hours"]
    return [{"period": period, **values} for period, values in sorted(periods.items())]


def size_period_totals(db, params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return each period's outcome counts and estimated hours of work, with no active time.

    The hours are the class midpoints of the accepted outcomes that have a size,
    so a reader that divides by something other than active time (Cost per hour
    of delivered work) shares the exact population of ``size_family``.
    """
    periods: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "outcomes": 0, "estimated_hours": 0.0, "basis": Counter(), "profiles": Counter(),
        **{status: 0 for status in STATUSES}, "unsized": 0,
    })
    for _, period, _, status, attributes, size_class in _sized_nodes(db, params):
        row = periods[period]
        row["outcomes"] += 1
        row[status] += 1
        if size_class is None:
            continue
        row["estimated_hours"] += CLASS_MIDPOINT_HOURS[size_class]
        row["basis"][attributes["basis"] if attributes else BASIS_AUTHOR_DECLARED] += 1
        row["profiles"][profile(attributes) if attributes else AUTHOR_DECLARED_PROFILE] += 1
    return periods


def _same_method(profiles: Any) -> bool:
    """Whether every profile is the same rating method.

    A profile is who rated, with which method and rubric, and which rater
    version. An unrecorded rater version says nothing about the method, so it
    matches any version; two recorded versions that differ, or any other
    differing key, are a real change in how outcomes were rated.
    """
    at = PROFILE_KEYS.index("rater_version")
    methods = {item[:at] + item[at + 1:] for item in profiles}
    versions = {item[at] for item in profiles} - {UNKNOWN_VERSION}
    return len(methods) <= 1 and len(versions) <= 1


def _comparable_reason(profiles: Counter, period_comparable: bool, range_profiles: set) -> str | None:
    """Name why a period cannot be compared with others, or None when it can."""
    if not period_comparable:
        return "outcome_definition_changed"
    if not _same_method(profiles):
        return "mixed_profiles"
    if not _same_method(range_profiles):
        return "profile_changes_in_range"
    return None


def _rate_reason(measured: bool, active_hours: float, sized: int, minimum_n: int) -> str | None:
    """Name why a period's rate is withheld, or None when it is served."""
    if not measured:
        return "before_data_start"
    if not active_hours:
        return "no_active_time"
    if not sized:
        return "no_usable_ratings"
    if sized < minimum_n:
        return "below_minimum"
    return None


def pooled_size_window(pooled: dict[str, Any], measured: bool, minimum_n: int) -> dict[str, Any]:
    """One comparison window's rate and n from its periods' summed counts, by the period rule.

    ``size`` is the window's pooled coverage: the counts behind the rate, so a reader that
    divides them another way (hours per rated outcome) reads this pool and never builds its own.
    """
    active_hours, sized = pooled.get("active_hours") or 0, int(pooled.get("usable") or 0)
    reason = _rate_reason(measured, active_hours, sized, minimum_n)
    excluded = sum(int(pooled.get(status) or 0) for status in (*STATUSES, "unsized") if status != "usable")
    return {"value": None if reason else pooled.get("estimated_hours", 0.0) / active_hours,
            "n": 0 if reason == "before_data_start" else sized,
            "size": {"outcomes": int(pooled.get("outcomes") or 0), "sized": sized, "excluded": excluded,
                     "estimated_hours": pooled.get("estimated_hours") or 0.0, "active_hours": active_hours}}


def size_rows(metric: str, minimum_n: int, spine: list[str], sources: list[dict[str, Any]],
              comparable: Any, measured: Any, *, dates: dict[str, str]) -> list[dict[str, Any]]:
    """Serve one row per completed period, including periods with no outcomes or time.

    The rate is null before the series' data start, when active time is absent
    or zero, or when fewer usable ratings than the registry minimum stand
    behind it; ``size.rate_reason`` names which. The known subtotal and the
    coverage counts are served either way, and a period where any outcome lacks
    a usable size is marked partial. ``dates`` carries the registry's data start
    and comparability break, so a reader names them without a second copy.
    """
    by_period = {row["period"]: row for row in sources}
    range_profiles = {item for row in sources if row["period"] in spine for item in row["profiles"]}
    rows = []
    for period in spine:
        source = by_period.get(period) or {"outcomes": 0, "estimated_hours": 0.0, "basis": Counter(),
                                       "profiles": Counter(), **{status: 0 for status in STATUSES}, "unsized": 0}
        sized, active_hours = source["usable"], source.get("active_hours") or 0
        rate_reason = _rate_reason(measured(metric, period), active_hours, sized, minimum_n)
        value = None if rate_reason else source["estimated_hours"] / active_hours
        # Before the minimum is applied: still a figure when the ratings are few, never when there are none.
        raw_value = source["estimated_hours"] / active_hours if rate_reason in (None, "below_minimum") else None
        n = 0 if rate_reason == "before_data_start" else sized
        reason = _comparable_reason(source["profiles"], comparable(metric, period), range_profiles) if sized else None
        reason = reason or (None if comparable(metric, period) else "outcome_definition_changed")
        rows.append({
            "period": period, "series": metric, "value": value, "raw_value": raw_value, "n": n, "proxy": True,
            "partial": sized < source["outcomes"],
            "comparable": reason is None, "break": None, "dimension": None, "thin": False,
            "size": {
                "outcomes": source["outcomes"], "sized": sized, "estimated_hours": source["estimated_hours"],
                "active_hours": active_hours, "unsized": source["unsized"],
                **{status: source[status] for status in STATUSES if status != "usable"},
                "basis": {name: source["basis"][name] for name in SERVED_BASES},
                "profiles": [dict(zip(PROFILE_KEYS, item), outcomes=count)
                             for item, count in sorted(source["profiles"].items())],
                "comparable_reason": reason, "rate_reason": rate_reason,
                "class_midpoint_hours": CLASS_MIDPOINT_HOURS, **dates,
            },
        })
    return rows
