"""Model-free, count-and-reference learning-status report producer."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

from loguru import logger

from core.corrections_recording import (
    CORRECTION_EVENT,
    LABEL_CORRECTION,
    SOURCE_KIND_DECISION_ANSWER,
    SOURCE_KIND_DOC_COMMENT,
    SOURCE_KIND_MESSAGE,
)
from core import workflow_verdict_evaluator
from core.workflow_verdict_read import PLANNED_EVENT, REACHED_EVENT
from events.learning_report import deliver_learning_report_sync
from plans.public import TERMINAL_STATES

EARLY_FLOOR = 5
HISTORY_DAYS = 28
MIN_HISTORY_DAYS = 14
MAX_REFS = 50
PERSON_CORRECTION_SOURCES = (
    SOURCE_KIND_MESSAGE,
    SOURCE_KIND_DOC_COMMENT,
    SOURCE_KIND_DECISION_ANSWER,
)
_PERSON_CORRECTION_SOURCE_PLACEHOLDERS = ", ".join("?" for _ in PERSON_CORRECTION_SOURCES)
_PERSON_CORRECTION_SOURCE_SQL = (
    "COALESCE(json_extract(attributes, '$.source_kind'), ?) "
    f"IN ({_PERSON_CORRECTION_SOURCE_PLACEHOLDERS})"
)
_PERSON_CORRECTION_SOURCE_ARGS = (SOURCE_KIND_MESSAGE, *PERSON_CORRECTION_SOURCES)


def _scope_where(scope: str) -> tuple[str, tuple[str, ...]]:
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
    source_args = _PERSON_CORRECTION_SOURCE_ARGS if person_only else ()
    return db.execute(
        f"""SELECT subject_key,agent_thread_id,ts,
                   COALESCE(json_extract(attributes, '$.source_kind'), ?) AS source_kind
              FROM outcome_events
             WHERE event_name=? AND retracted_at IS NULL
               AND json_extract(attributes, '$.label')=?
               AND ts>=? AND ts<?{source_where} AND {where}
             ORDER BY source_kind,subject_key""",
        (SOURCE_KIND_MESSAGE, CORRECTION_EVENT, LABEL_CORRECTION, start, end, *source_args, *scope_args),
    ).fetchall()


def _lessons(
    db: sqlite3.Connection, scope: str, report_date: str,
) -> tuple[dict[str, dict[str, Any] | None], set[str]]:
    where, scope_args = _scope_where(scope)
    start, end = _day_bounds(report_date)
    rows = db.execute(
        f"""SELECT l.source_event_id,p.owner_id,n.state
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
    # a review key is lesson:<id>:<sha> for a trial, lesson:<id>:watch:<sha> for a watch,
    # capture:<id>:<hash>; a capture that became a trial is closed, or shares the trial's id
    open_keys = [str(row["source_event_id"]).split(":") for row in rows if row["state"] not in TERMINAL_STATES]
    in_use = {key[1] for key in open_keys if key[0] == "lesson" and len(key) == 3}
    held = {key[1] for key in open_keys} - in_use
    in_use_refs = [ref for ref in refs if ref.split(":")[1] in in_use]
    held_refs = [ref for ref in refs if ref.split(":")[1] in held]
    return ({
        "recorded": {**_count(refs), "count": len(in_use | held)},
        "in_use": {**_count(in_use_refs), "count": len(in_use)},
        "held": {**_count(held_refs), "count": len(held)},
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
    # a person's correction day wakes the daily read: its floor line is the report's only post
    wake = report_kind != "daily" or len(lesson_agents) >= 2 or any(
        source in PERSON_CORRECTION_SOURCES for source in correction_refs
    ) or any(count is not None and count["count"] > 0 for count in experiments.values())
    return {"scope": scope, "report_date": report_date, "report_key": f"{scope}:{report_date}:{report_kind}",
            "wake": wake, "items": items}


def early_threshold(db: sqlite3.Connection, *, scope: str, report_date: str) -> int:
    """The prior 28 local-install days' p90 of corrections, or the history floor."""
    day = date.fromisoformat(report_date)
    start, end = _day_bounds((day - timedelta(days=HISTORY_DAYS)).isoformat())[0], _day_bounds(report_date)[0]
    first = db.execute(
        f"""SELECT MIN(ts) FROM outcome_events
             WHERE event_name=? AND retracted_at IS NULL AND project_id=?
               AND json_extract(attributes, '$.label')=?
               AND {_PERSON_CORRECTION_SOURCE_SQL}""",
        (CORRECTION_EVENT, scope, LABEL_CORRECTION, *_PERSON_CORRECTION_SOURCE_ARGS),
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
        (CORRECTION_EVENT, scope, LABEL_CORRECTION, start, end, *_PERSON_CORRECTION_SOURCE_ARGS),
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
    corrections = _correction_rows(db, scope, report_date, person_only=True)
    threshold = early_threshold(db, scope=scope, report_date=report_date)
    kind = "early-2" if len(corrections) >= 2 * threshold else "early-1" if len(corrections) >= threshold else None
    if kind is None:
        return False
    deliver_learning_report_sync(build_report(db, scope=scope, report_date=report_date, report_kind=kind))
    return True


def _has_reportable_items(report: dict[str, Any]) -> bool:
    items = report["items"]
    counts = [
        *items["corrections"].values(), *items["lessons"].values(),
        *items["experiments"].values(), items["wins"],
    ]
    return any(count is not None and count["count"] > 0 for count in counts)


def emit_daily_reports(*, now: datetime | None = None) -> list[str]:
    """Cover the last two completed local days; duplicate report keys are harmless."""
    from infra.database import get_db

    instant = now or datetime.now(timezone.utc)
    current_day = instant.astimezone(install_timezone()).date()
    report_dates = tuple((current_day - timedelta(days=offset)).isoformat() for offset in (2, 1))
    db = get_db()
    try:
        scopes = [str(row["id"]) for row in db.execute("SELECT id FROM projects ORDER BY id")]
        reports = []
        for report_date in report_dates:
            for scope in scopes:
                try:
                    report = build_report(db, scope=scope, report_date=report_date)
                except Exception:
                    logger.exception("learning report build failed for {} on {}", scope, report_date)
                else:
                    if _has_reportable_items(report):
                        reports.append(report)
    finally:
        db.close()
    delivered = []
    for report in reports:
        try:
            deliver_learning_report_sync(report)
        except Exception:
            logger.exception("learning report delivery failed for {}", report["report_key"])
        else:
            delivered.append(str(report["report_key"]))
    return delivered
