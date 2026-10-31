"""The corrections family: the person's corrections per period beside the asks already counted.

Corrections are the classified owner sources ``core.correction_attribution`` resolves, counted
in the period each arrived and in the scope's threads, the way an ask counts in the period it
was raised. The family also carries the decisions family's ask counts, so the combined ratio
adds the one ask definition rather than a second one.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from api.measurement_breaks import local_date
from api.measurement_prepared import PREPARED_SCOPE
from core.correction_attribution import person_correction_coverage
from core.measurement_scope import Grain


# The series served from the coverage fields; the period in progress is served too, flagged partial.
COVERAGE_METRICS = frozenset({
    "corrections_detected", "corrections_triaged_pct", "corrections_untriaged_idle_pct", "idle_untriaged_threads",
})


def coverage_value(metric: str, source: dict[str, Any]) -> tuple[float | int | None, int]:
    """One coverage series' value and the n behind it, from a family row: a count is its own n, a share's n is its denominator."""
    def get(name: str) -> int:
        return int(source.get(name, 0) or 0)

    if metric == "corrections_detected": return get("detected"), get("detected")
    if metric == "idle_untriaged_threads": return get("idle_threads"), get("idle_threads")
    numerator, n = (get("triaged"), get("detected")) if metric == "corrections_triaged_pct" else (get("untriaged_idle"), get("untriaged"))
    return (numerator / n if n else None), n


def corrections_by_period(db, grain: Grain) -> dict[str, Counter[str]]:
    """Per drawn period (and the one in progress), the corrections whose target thread is in the scope.

    ``detected`` counts them all; ``triaged`` and ``untriaged`` split them; ``untriaged_idle`` are the
    untriaged whose thread has started no turn since, and ``idle_threads`` is how many threads those sit on.
    """
    in_scope = {str(row[0]) for row in db.execute(f"SELECT id FROM {PREPARED_SCOPE}")}
    drawn = {*grain.periods, grain.current}
    counts: dict[str, Counter[str]] = {}
    idle: dict[str, set[str]] = {}
    for item in person_correction_coverage(db, grain.starts[0], grain.current_end):
        period = grain.period_of(local_date(grain, item.arrived))
        if item.thread not in in_scope or period not in drawn:
            continue
        row = counts.setdefault(period, Counter())
        row["detected"] += 1
        row["triaged" if item.triaged else "untriaged"] += 1
        if not item.triaged and not item.later_turn:
            row["untriaged_idle"] += 1
            idle.setdefault(period, set()).add(str(item.thread))
    for period, threads in idle.items():
        counts[period]["idle_threads"] = len(threads)
    return counts


def corrections_family(db, grain: Grain, decision_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per outcome period: its outcomes, asks (raised and user holds) and corrections."""
    counts = corrections_by_period(db, grain)
    fields = ("detected", "triaged", "untriaged", "untriaged_idle", "idle_threads")
    rows = [{**row, "corrections": counts.get(row["period"], Counter())["detected"],
             **{name: counts.get(row["period"], Counter())[name] for name in fields}} for row in decision_rows]
    known = {row["period"] for row in rows}
    # The period in progress may have corrections before it has an outcome row.
    rows.extend({"period": period, "corrections": count["detected"], **{name: count[name] for name in fields}}
                for period, count in counts.items() if period not in known)
    return rows
