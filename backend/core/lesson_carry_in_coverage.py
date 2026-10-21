"""Coverage accounting for carry-in outcome facts."""
from __future__ import annotations

import json

from core.lesson_carry_in import RESULT_EVENT, SOURCE_SKIPPED_EVENT


def coverage_counts(db) -> dict[str, int]:
    """Report covered/skipped/total without claiming every eligible source ran."""
    rows = db.execute(
        "SELECT event_name,attributes FROM outcome_events WHERE event_name IN (?, ?) AND retracted_at IS NULL",
        (RESULT_EVENT, SOURCE_SKIPPED_EVENT),
    ).fetchall()
    covered = skipped = 0
    for row in rows:
        try:
            attrs = json.loads(row["attributes"])
        except (TypeError, json.JSONDecodeError):
            continue
        if row["event_name"] == SOURCE_SKIPPED_EVENT:
            skipped += 1
        elif attrs.get("role") != "rebaseline":
            covered += 1
    return {"covered": covered, "skipped": skipped, "total": covered + skipped}
