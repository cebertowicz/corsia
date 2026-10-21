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
from infra.measure_admission import UNNAMED_LINE, admit, refuse_line
from infra.outcome_events import count_outcome_event

if TYPE_CHECKING:
    from automation.script_readings import RunReport

WORKSPACE_SCOPE = "__workspace__"


@dataclass(frozen=True)
class MeasureOutcome:
    recorded: int = 0
    refused: int = 0
    accepted: int = 0  # lines admission took, including ones deduped against an earlier row

    @property
    def line(self) -> str:
        return f"measures: {self.recorded} recorded, {self.refused} refused (measure_status)"


def settle_measures(db: sqlite3.Connection, schedule: Schedule, report: RunReport) -> MeasureOutcome:
    """Admit the run's measure lines and count its refusals; never raises for a bad line.

    The report's ``workflow_stamp`` was resolved before settling. A schedule that names a
    workflow but has no versioned stamp (no longer the owner's, or no version) records its rows
    without one and counts each as ``unversioned``.
    """
    workflow = (schedule.payload or {}).get("workflow")
    ref, version = report.workflow_stamp or (None, None)
    if report.measures and report.run_id is None:
        raise ValueError("a run report with measure lines needs the runner's run_id")
    project = None if schedule.scope_id == WORKSPACE_SCOPE else schedule.scope_id
    recorded = accepted = 0
    for measure in report.measures:
        admission = admit(
            db, owner_thread_id=schedule.thread_id, event_name=measure["event.name"],
            attributes=measure.get("attributes", {}), project_id=project,
            run_id=f"script:{schedule.id}:{report.run_id}", workflow_ref=ref, workflow_version=version,
            ts=measure.get("time"),
        )
        if admission.refusal is None:
            accepted += 1
            inserted = bool(admission.result and admission.result.inserted)
            recorded += int(inserted)
            if inserted and workflow and version is None:
                count_outcome_event(db, "unversioned", measure["event.name"], agent_thread_id=schedule.thread_id)
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
