"""What settling a script run does with its measure lines: admit, refuse, and say what happened.

Admission is the same function the OTLP path uses; this module only feeds it the run's
lines and totals the outcome for the run's line in ``automation_list``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from automation.schedules import Schedule
from automation.script_lines import MAX_MEASURE_LINES, UNNAMED_LINE

if TYPE_CHECKING:
    from automation.script_readings import RunReport

WORKSPACE_SCOPE = "__workspace__"


@dataclass(frozen=True)
class MeasureOutcome:
    recorded: int = 0
    refused: int = 0
    accepted: int = 0  # lines admission took, including ones deduped against an earlier row
    workflow_stale: bool = False

    @property
    def line(self) -> str:
        return f"measures: {self.recorded} recorded, {self.refused} refused (measure_status)"

    @property
    def payload(self) -> dict[str, object]:
        return {"last_measures": self.line, "workflow_stale": self.workflow_stale}


def settle_measures(db: sqlite3.Connection, schedule: Schedule, report: RunReport) -> MeasureOutcome:
    """Admit the run's measure lines and count its refusals; never raises for a bad line."""
    from infra.measure_admission import admit, refuse_line, workflow_is_owned

    workflow = (schedule.payload or {}).get("workflow")
    stale = bool(workflow) and not workflow_is_owned(db, schedule.thread_id, workflow)
    if report.measures and report.run_id is None:
        raise ValueError("a run report with measure lines needs the runner's run_id")
    project = None if schedule.scope_id == WORKSPACE_SCOPE else schedule.scope_id
    recorded = accepted = 0
    for measure in report.measures:
        admission = admit(
            db, owner_thread_id=schedule.thread_id, event_name=measure["event.name"],
            attributes=measure.get("attributes", {}), project_id=project,
            run_id=f"script:{schedule.id}:{report.run_id}", workflow=workflow, ts=measure.get("time"),
        )
        if admission.refusal is None:
            accepted += 1
            recorded += int(bool(admission.result and admission.result.inserted))
    for refusal in report.measure_refusals:
        refuse_line(db, schedule.thread_id, refusal.text, event_name=refusal.event_name)
    if report.measures_over_cap:
        refuse_line(
            db, schedule.thread_id,
            f"{report.measures_over_cap} measure line(s) past the {MAX_MEASURE_LINES}-line cap were dropped; print fewer per run.",
            event_name=UNNAMED_LINE, count=report.measures_over_cap,
        )
    refused = len(report.measure_refusals) + report.measures_over_cap + (len(report.measures) - accepted)
    return MeasureOutcome(recorded, refused, accepted, stale)
