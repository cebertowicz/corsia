"""Model-free, count-and-reference learning-status report producer."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

from core.corrections_recording import CORRECTION_EVENT, LABEL_CORRECTION
from core import workflow_verdict_evaluator
from core.workflow_verdict_read import PLANNED_EVENT, REACHED_EVENT

EARLY_FLOOR = 5
HISTORY_DAYS = 28
MIN_HISTORY_DAYS = 14
MAX_REFS = 50
PERSON_CORRECTION_SOURCES = ("message", "doc_comment", "plan_decision_answer")
_PERSON_CORRECTION_SOURCE_SQL = "COALESCE(json_extract(attributes, '$.source_kind'), 'message') IN (?,?,?)"


def deliver_learning_report(report: dict[str, Any]) -> Any:
    """Synchronously validate and deliver from the producer's synchronous jobs."""
    from events.learning_report import deliver_learning_report_sync

    return deliver_learning_report_sync(report)


def _scope_where(scope: str) -> tuple[str, tuple[str, ...]]:
    if scope == "workspace":
        # Cross-project adoption/theme records have no durable producer source yet.
        return "0", ()
    return "project_id=?", (scope,)


def install_timezone() -> tzinfo:
    """The install's local zone; CORSIA has no persisted timezone setting on dev."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def _day_bounds(report_date: str) -> tuple[str, str]:
    """UTC instants enclosing one local install calendar day, used by every report read."""
    day = date.fromisoformat(report_date)
    zone = install_timezone()
    start = datetime.combine(day, time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()


def report_date_for(now: datetime) -> str:
    """The local-install report day containing one instant."""
    return now.astimezone(install_timezone()).date().isoformat()


def _count(refs: list[str]) -> dict[str, Any]:
    return {"count": len(refs), "refs": refs[:MAX_REFS]}


def _correction_rows(
    db: sqlite3.Connection, scope: str, report_date: str, *, person_only: bool = False,
) -> list[sqlite3.Row]:
    where, scope_args = _scope_where(scope)
    start, end = _day_bounds(report_date)
    source_where = f" AND {_PERSON_CORRECTION_SOURCE_SQL}" if person_only else ""
    source_args = PERSON_CORRECTION_SOURCES if person_only else ()
    return db.execute(
        f"""SELECT subject_key,agent_thread_id,ts,
                   COALESCE(json_extract(attributes, '$.source_kind'), 'message') AS source_kind
              FROM outcome_events
             WHERE event_name=? AND retracted_at IS NULL
               AND json_extract(attributes, '$.label')=?
               AND ts>=? AND ts<?{source_where} AND {where}
             ORDER BY source_kind,subject_key""",
        (CORRECTION_EVENT, LABEL_CORRECTION, start, end, *source_args, *scope_args),
    ).fetchall()


def _lessons(
    db: sqlite3.Connection, scope: str, report_date: str,
) -> tuple[dict[str, dict[str, Any] | None], set[str]]:
    where, scope_args = _scope_where(scope)
    start, end = _day_bounds(report_date)
    rows = db.execute(
        f"""SELECT l.source_event_id,p.owner_id
              FROM plan_node_artifact_links l
              JOIN plan_nodes n ON n.id=l.node_id
              JOIN plans p ON p.id=n.plan_id
              JOIN threads t ON t.id=p.owner_id
             WHERE l.provenance='lesson.trial_review' AND n.created_at>=? AND n.created_at<?
               AND {where}
             ORDER BY l.source_event_id""",
        (start, end, *scope_args),
    ).fetchall()
    # trials only until the lesson gate routes every lesson through place
    refs = [str(row["source_event_id"]) for row in rows]
    return ({
        "recorded": _count(refs),
        "confirmed": None,
        "consolidated": None,
        "repeated": None,
    }, {str(row["owner_id"]) for row in rows})


def _experiment_count(
    db: sqlite3.Connection, scope: str, report_date: str, event_name: str,
) -> dict[str, Any]:
    where, scope_args = _scope_where(scope)
    start, end = _day_bounds(report_date)
    rows = db.execute(
        f"""SELECT subject_key FROM outcome_events
             WHERE event_name=? AND retracted_at IS NULL AND ts>=? AND ts<? AND {where}
             ORDER BY subject_key""",
        (event_name, start, end, *scope_args),
    ).fetchall()
    return _count([str(row["subject_key"]) for row in rows])


def build_report(db: sqlite3.Connection, *, scope: str, report_date: str, report_kind: str = "daily") -> dict[str, Any]:
    """Build one report from durable facts, with no model call or write."""
    if report_kind not in {"daily", "early-1", "early-2"}:
        raise ValueError("report_kind must be daily, early-1, or early-2")
    corrections = _correction_rows(db, scope, report_date)
    correction_refs: dict[str, list[str]] = defaultdict(list)
    for row in corrections:
        correction_refs[str(row["source_kind"])].append(str(row["subject_key"]))
    lessons, lesson_agents = _lessons(db, scope, report_date)
    experiments = (
        {
            "started": _experiment_count(db, scope, report_date, PLANNED_EVENT),
            "decided": _experiment_count(db, scope, report_date, REACHED_EVENT),
        }
        if workflow_verdict_evaluator.VERDICT_WRITES_ENABLED
        else {"started": None, "decided": None}
    )
    items = {
        "corrections": {source: _count(refs) for source, refs in sorted(correction_refs.items())},
        "lessons": lessons,
        "experiments": experiments,
        "wins": None,
    }
    wake = report_kind != "daily" or len(lesson_agents) >= 2 or any(
        count is not None and count["count"] > 0 for count in experiments.values()
    )
    return {"scope": scope, "report_date": report_date, "report_key": f"{scope}:{report_date}:{report_kind}",
            "wake": wake, "items": items}


def early_threshold(db: sqlite3.Connection, *, scope: str, report_date: str) -> int:
    """The prior 28 local-install days' p90 of corrections, or the history floor."""
    day = date.fromisoformat(report_date)
    if scope == "workspace":
        return EARLY_FLOOR
    start, end = _day_bounds((day - timedelta(days=HISTORY_DAYS)).isoformat())[0], _day_bounds(report_date)[0]
    first = db.execute(
        f"""SELECT MIN(ts) FROM outcome_events
             WHERE event_name=? AND retracted_at IS NULL AND project_id=?
               AND json_extract(attributes, '$.label')=?
               AND {_PERSON_CORRECTION_SOURCE_SQL}""",
        (CORRECTION_EVENT, scope, LABEL_CORRECTION, *PERSON_CORRECTION_SOURCES),
    ).fetchone()[0]
    if first is None:
        return EARLY_FLOOR
    first_day = report_date_for(datetime.fromisoformat(str(first).replace("Z", "+00:00")))
    if (day - date.fromisoformat(first_day)).days < MIN_HISTORY_DAYS:
        return EARLY_FLOOR
    rows = db.execute(
        f"""SELECT ts FROM outcome_events
             WHERE event_name=? AND retracted_at IS NULL AND project_id=?
               AND json_extract(attributes, '$.label')=? AND ts>=? AND ts<?
               AND {_PERSON_CORRECTION_SOURCE_SQL}""",
        (CORRECTION_EVENT, scope, LABEL_CORRECTION, start, end, *PERSON_CORRECTION_SOURCES),
    ).fetchall()
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[report_date_for(datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00")))] += 1
    values = sorted(
        counts[(day - timedelta(days=offset)).isoformat()]
        for offset in range(1, HISTORY_DAYS + 1)
    )
    return max(EARLY_FLOOR, values[math.ceil(0.9 * len(values)) - 1])


def maybe_emit_early(db: sqlite3.Connection, *, scope: str, report_date: str) -> bool:
    """Deliver the count-derived early key; repeat calls stay idempotent by key."""
    if scope == "workspace":
        return False
    corrections = _correction_rows(db, scope, report_date, person_only=True)
    threshold = early_threshold(db, scope=scope, report_date=report_date)
    kind = "early-2" if len(corrections) >= 2 * threshold else "early-1" if len(corrections) >= threshold else None
    if kind is None:
        return False
    deliver_learning_report(build_report(db, scope=scope, report_date=report_date, report_kind=kind))
    return True


def emit_daily_reports(*, now: datetime | None = None) -> list[str]:
    """Run on the existing nightly job for the last completed local-install day."""
    from infra.database import get_db

    instant = now or datetime.now(timezone.utc)
    completed_day = (instant.astimezone(install_timezone()).date() - timedelta(days=1)).isoformat()
    db = get_db()
    try:
        scopes = ["workspace", *(str(row["id"]) for row in db.execute("SELECT id FROM projects ORDER BY id"))]
        reports = [build_report(db, scope=scope, report_date=completed_day) for scope in scopes]
    finally:
        db.close()
    delivered = []
    for report in reports:
        items = report["items"]
        counts = [
            *items["corrections"].values(), *items["lessons"].values(),
            *items["experiments"].values(), items["wins"],
        ]
        if any(count is not None and count["count"] > 0 for count in counts):
            delivered.append(report)
    for report in delivered:
        deliver_learning_report(report)
    return [str(report["report_key"]) for report in delivered]
