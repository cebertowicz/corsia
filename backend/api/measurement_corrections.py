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
from core.correction_attribution import person_corrections
from core.measurement_scope import Grain


def corrections_by_period(db, grain: Grain) -> Counter[str]:
    """Corrections per drawn period (and the one in progress) whose target thread is in the scope."""
    in_scope = {str(row[0]) for row in db.execute(f"SELECT id FROM {PREPARED_SCOPE}")}
    drawn = {*grain.periods, grain.current}
    counts: Counter[str] = Counter()
    for arrived, thread in person_corrections(db, grain.starts[0], grain.current_end):
        period = grain.period_of(local_date(grain, arrived))
        if thread in in_scope and period in drawn:
            counts[period] += 1
    return counts


def corrections_family(db, grain: Grain, decision_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per outcome period: its outcomes, asks (raised and user holds) and corrections."""
    counts = corrections_by_period(db, grain)
    return [{**row, "corrections": counts.get(row["period"], 0)} for row in decision_rows]
