"""Read daily machinery facts as Measurement periods.

A UTC day belongs to the viewer period containing its 12:00 UTC instant.
A whole skill read is counted at its file's size. What the tool returned can
be smaller where text was masked and larger by the line numbers it adds; a
partial read adds no size. Both returned-size shares are proxies, and a period
whose numerator exceeds its total is not measured. Token attribution is
proportional to each thread-day's actions.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone

from loguru import logger

from api.measurement_breaks import local_date
from api.measurement_prepared import PREPARED_OUTCOMES, PREPARED_SCOPE
from core.machinery_cost import BUCKETS, DAY_EVENT, RECORD_EVENT, STEMS


def machinery_value(metric, source):
    """Return this metric's value and denominator count from a period or pooled row."""
    if metric == "ungrouped_action_pct":
        numerator, denominator = source.get("ungrouped_count", 0), source.get("actions", 0)
        return (numerator / denominator if denominator else None), denominator
    stem, suffix = next((stem, metric[len(stem) + 1:]) for stem in STEMS if metric.startswith(stem + "_"))
    if suffix == "action_pct":
        numerator, denominator = source.get(f"{stem}_count", 0), source.get("actions", 0)
    elif suffix == "returned_pct":
        numerator, denominator = source.get(f"{stem}_bytes", 0), source.get("bytes", 0)
    elif suffix == "token_pct":
        numerator, denominator = source.get(f"{stem}_tokens", 0), source.get("tokens", 0)
    else:
        numerator, denominator = source.get(f"{stem}_tokens", 0), source.get("outcomes", 0)
    if suffix == "returned_pct" and stem in {"skill_read", "machinery"} and numerator > denominator:
        return None, 0
    n = source.get("outcomes", 0) if suffix == "tokens_per_outcome" else source.get("actions", 0)
    return (numerator / denominator if denominator else None), int(n)


def machinery_family(db, grain):
    """Return only periods whose every UTC-day marker exists, scoped to selected threads."""
    first = date.fromisoformat(grain.starts[0][:10]) - timedelta(days=2)
    last = date.fromisoformat(grain.current_end[:10]) + timedelta(days=2)
    def stamp(day):
        return datetime.combine(day, time(12), timezone.utc).isoformat()
    def period(ts):
        return grain.period_of(local_date(grain, ts))
    expected = defaultdict(set)
    day = first
    while day <= last:
        key = period(stamp(day))
        if key in grain.periods:
            expected[key].add(str(day))
        day += timedelta(days=1)
    markers = defaultdict(set)
    for row in db.execute("SELECT ts FROM outcome_events WHERE event_name=? AND ts>=? AND ts<? AND retracted_at IS NULL",
                          (DAY_EVENT, stamp(first), stamp(last + timedelta(days=1)))):
        markers[period(row["ts"])].add(row["ts"][:10])
    complete = {p for p, days in expected.items() if days and days <= markers[p]}
    totals = {p: Counter() for p in complete}
    for row in db.execute(
        f"SELECT ts,attributes FROM outcome_events WHERE event_name=? AND ts>=? AND ts<? "
        f"AND retracted_at IS NULL AND agent_thread_id IN (SELECT id FROM {PREPARED_SCOPE})",
        (RECORD_EVENT, stamp(first), stamp(last + timedelta(days=1))),
    ):
        p = period(row["ts"])
        if p not in complete:
            continue
        fact = json.loads(row["attributes"])
        count = sum(fact.get(f"c_{b}", 0) for b in BUCKETS)
        size = sum(fact.get(f"b_{b}", 0) for b in BUCKETS)
        groups = {"plan": (fact.get("c_plans", 0), fact.get("b_plans", 0)),
                  "memory": (fact.get("c_memory", 0), fact.get("b_memory", 0)),
                  "agents": (fact.get("c_delegation", 0), fact.get("b_delegation", 0)),
                  "skill_read": (fact.get("skill_count", 0), fact.get("skill_bytes", 0))}
        groups["machinery"] = tuple(sum(item[i] for item in groups.values()) for i in (0, 1))
        target = totals[p]
        tokens = int(fact.get("fresh_tokens", 0))
        target.update({"actions": count, "bytes": size, "tokens": tokens,
                       "ungrouped_count": fact.get("c_ungrouped", 0)})
        for stem, (actions, returned) in groups.items():
            target[f"{stem}_count"] += actions
            target[f"{stem}_bytes"] += returned
            target[f"{stem}_tokens"] += tokens * actions / count if count else 0
    for p, total in totals.items():
        for stem in ("skill_read", "machinery"):
            numerator = total[f"{stem}_bytes"]
            if numerator > total["bytes"]:
                logger.warning("machinery_cost: id={} period={} numerator={} total={}",
                               f"{stem}_returned_pct", p, numerator, total["bytes"])
    outcomes = {r["period"]: r["outcomes"] for r in db.execute(f"SELECT period,outcomes FROM {PREPARED_OUTCOMES}")}
    return [{"period": p, **totals[p], "outcomes": outcomes.get(p, 0)} for p in sorted(complete)]
