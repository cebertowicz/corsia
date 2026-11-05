"""What settling a script run does with its measure lines: admit, refuse, and say what happened.

Admission is ``infra.measure_admission.admit``, the one path declared events take; this module
only feeds it the run's lines and totals the outcome for the schedule's ``last_measures``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from automation.schedules import Schedule
from automation.script_lines import MAX_MEASURE_LINES
from infra.measure_admission import UNNAMED_LINE, admit, count_unversioned, refuse_line

if TYPE_CHECKING:
    from automation.script_readings import RunReport
    from events.intake import MeasureStamps

WORKSPACE_SCOPE = "__workspace__"


@dataclass(frozen=True)
class MeasureOutcome:
    recorded: int = 0
    refused: int = 0
    accepted: int = 0  # lines admission took, including ones deduped against an earlier row

    @property
    def line(self) -> str:
        return f"measures: {self.recorded} recorded, {self.refused} refused (measure_status)"


def run_stamps(schedule: Schedule, report: RunReport) -> MeasureStamps:
    """The one derivation of a script run's record stamps, for its measure lines and its event lines alike.

    A schedule that names a workflow but has no versioned stamp (no longer the owner's, or no
    version) is ``unversioned``: its rows record without a stamp and are counted.
    """
    from events.intake import MeasureStamps

    ref, version = report.workflow_stamp or (None, None)
    return MeasureStamps(
        source="agent", provenance="agent_recorded",
        run_id=f"script:{schedule.id}:{report.run_id}", workflow_ref=ref, workflow_version=version,
        project_id=None if schedule.scope_id == WORKSPACE_SCOPE else schedule.scope_id,
        unversioned=bool((schedule.payload or {}).get("workflow")) and version is None,
    )


def settle_measures(db: sqlite3.Connection, schedule: Schedule, report: RunReport) -> MeasureOutcome:
    """Admit the run's measure lines and count its refusals; never raises for a bad line.

    The report's ``workflow_stamp`` was resolved before settling. A schedule that names a
    workflow but has no versioned stamp (no longer the owner's, or no version) records its rows
    without one and counts each as ``unversioned``.
    """
    if report.measures and report.run_id is None:
        raise ValueError("a run report with measure lines needs the runner's run_id")
    stamps = run_stamps(schedule, report)
    recorded = accepted = 0
    for measure in report.measures:
        admission = admit(
            db, owner_thread_id=schedule.thread_id, event_name=measure["event.name"],
            attributes=measure.get("attributes", {}), source=stamps.source, provenance=stamps.provenance,
            project_id=stamps.project_id,
            run_id=stamps.run_id, workflow_ref=stamps.workflow_ref, workflow_version=stamps.workflow_version,
            ts=measure.get("time"),
        )
        if admission.refusal is None:
            accepted += 1
            inserted = bool(admission.result and admission.result.inserted)
            recorded += int(inserted)
            if stamps.unversioned:
                count_unversioned(db, admission, measure["event.name"], schedule.thread_id)
    for refusal in report.measure_refusals:
        refuse_line(db, schedule.thread_id, refusal.text, event_name=refusal.event_name)
    if report.measures_over_cap:
        refuse_line(
            db, schedule.thread_id,
            f"{report.measures_over_cap} measure or event line(s) past the {MAX_MEASURE_LINES}-line cap were dropped; print fewer per run.",
            event_name=UNNAMED_LINE, count=report.measures_over_cap,
        )
    refused = len(report.measure_refusals) + report.measures_over_cap + (len(report.measures) - accepted)
    return MeasureOutcome(recorded, refused, accepted)
