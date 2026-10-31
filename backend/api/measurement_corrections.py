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
    if metric == "idle_untriaged_threads":
        threads = len(source.get("idle_threads") or ())
        return threads, threads
    numerator, n = (get("triaged"), get("detected")) if metric == "corrections_triaged_pct" else (get("untriaged_idle"), get("untriaged"))
    return (numerator / n if n else None), n


def corrections_by_period(db, grain: Grain) -> dict[str, dict[str, Any]]:
    """Per drawn period (and the one in progress), the corrections whose target thread is in the scope.

    ``detected`` counts them all; ``triaged`` and ``untriaged`` split them; ``untriaged_idle`` are the
    untriaged no turn has had since, and ``idle_threads`` is the set of threads those sit on, so a
    window or average pooling several periods counts a thread idle in more than one of them once.
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
    return {period: {**count, "idle_threads": frozenset(idle.get(period, ()))} for period, count in counts.items()}


def corrections_family(db, grain: Grain, decision_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per outcome period: its outcomes, asks (raised and user holds) and corrections."""
    counts = corrections_by_period(db, grain)
    empty = {"detected": 0, "triaged": 0, "untriaged": 0, "untriaged_idle": 0, "idle_threads": frozenset()}

    def fields(period: str) -> dict[str, Any]:
        count = {**empty, **counts.get(period, {})}
        return {"corrections": count["detected"], **count}

    rows = [{**row, **fields(row["period"])} for row in decision_rows]
    known = {row["period"] for row in rows}
    # The period in progress may have corrections before it has an outcome row.
    rows.extend({"period": period, **fields(period)} for period in counts if period not in known)
    return rows
