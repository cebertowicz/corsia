"""Model-free daily check of declared agent KPIs against their review lines (Agent KPIs design §4.7).

The nightly scheduler owns the once-per-day cadence. Each agent with an alerting KPI is read outside any
transaction; its moves go to it in one ``agent:<thread id>`` report (a wake when any move is ``alert: wake``),
and the same moves are listed, with no breach, in the day's project report so the conductor sees them routed.
A report's key is its scope and day, so a second run the same day delivers nothing new.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone

from loguru import logger

from api.measurement_agent_kpis import _read
from api.measurement_partial import grain_instant
from api.measurement_scope import resolve_grain
from events.movement_report import AGENT_SCOPE_PREFIX, Move, deliver_movement_report_sync
from infra.database import get_db
from jobs.kpi_moves import KpiMove, kpi_moves
from jobs.learning_report import report_date_for

# Completed weeks read per agent: enough for the newest one that has passed its settle days.
WEEKS = 4
WORKSPACE_SCOPE = "workspace"


def _owners(db) -> list[tuple[str, str, str | None]]:
    return [(row["id"], row["user_id"], row["project_id"]) for row in db.execute(
        """SELECT DISTINCT t.id, t.user_id, t.project_id FROM measure_definitions d
             JOIN threads t ON t.id = d.owner_thread_id
            WHERE d.kind='kpi' AND d.removed=0 AND json_extract(d.spec_json, '$.alert') IN ('wake', 'digest')
              AND COALESCE(t.agent_enabled,0)=1 AND COALESCE(t.agent_status,'active')='active'
            ORDER BY t.id""")]


def _report(scope: str, day: str, moves: list[Move], breach: bool) -> dict:
    return {"scope": scope, "report_date": day, "breach": breach, "moves": [m.as_payload() for m in moves]}


def run_daily(*, now: datetime | None = None) -> list[str]:
    """Deliver today's KPI moves; returns the keys of the reports that went to an agent."""
    instant = now or datetime.now(timezone.utc)
    day = report_date_for(instant)
    weeks = resolve_grain("weeks", "UTC", now=instant)
    grain = replace(weeks, periods=weeks.periods[-WEEKS:], starts=weeks.starts[-(WEEKS + 1):])
    bounds = [grain_instant(s) for s in (*grain.starts, grain.current_end)]
    by_owner: dict[str, tuple[str | None, list[KpiMove]]] = {}
    db = get_db()
    try:
        for owner, user_id, project in _owners(db):
            try:
                result = _read(user_id, project or "all", owner, grain)
                moves = kpi_moves(db, owner, result, bounds, instant)
            except Exception:
                logger.exception("kpi threshold read failed for agent {}", owner)
                continue
            if moves:
                by_owner[owner] = (project, moves)
    finally:
        db.close()
    delivered: list[str] = []
    listed: defaultdict[str, list[Move]] = defaultdict(list)
    for owner, (project, moves) in by_owner.items():
        scope = f"{AGENT_SCOPE_PREFIX}{owner}"
        try:
            deliver_movement_report_sync(
                _report(scope, day, [m.move for m in moves], any(m.wake for m in moves)))
        except Exception:
            logger.exception("kpi threshold report failed for agent {}", owner)
            continue
        delivered.append(f"{scope}:{day}")
        listed[project or WORKSPACE_SCOPE] += [m.move for m in moves]
    for scope, moves in listed.items():
        try:
            deliver_movement_report_sync(_report(scope, day, moves, False))
        except Exception:
            logger.exception("kpi threshold project report failed for {}", scope)
    logger.info("kpi_thresholds: agents={} reports={}", len(by_owner), len(delivered))
    return delivered
