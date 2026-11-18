"""Say when a rollup refresh leaves Measurement reading raw rows past the rollup.

The usage and focus-attention rollups are caches (``api.usage_rollup``, ``api.presence_rollup``):
a read is exact whatever their state, so a refresh that never catches up shows nowhere except as
slow Measurement reads. After each refresh this reads how far each rollup covers and, when either
is more than ``LAG_WARNING`` behind, logs one WARNING (at most every 15 minutes per process) with
the coverage, how the refresh ended and the oldest open visit, which bounds the focus rollup.

This is a temporary diagnostic for issue #2759, not a second rollup mechanism. It should stop
firing once that issue's root-cause fix ships; delete this module and its call in
``api.usage_rollup`` once production has logged no such warning for 7 days after that fix.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from api import presence_rollup
from infra.database import get_db
from infra.usage_rollup_schema import utc_now

logger = logging.getLogger(__name__)

#: A rollup further behind than this makes Measurement read that much raw.
LAG_WARNING = timedelta(hours=6)
_EVERY_SECONDS = 15 * 60.0
_last_warning = [float("-inf")]


def coverage_job() -> tuple[str, int, str, str]:
    """Usage built_through and dirty hours, focus built_through and the oldest open visit ('' for none)."""
    from api.usage_rollup import rollup_through

    db = get_db()
    try:
        dirty = db.execute("SELECT COUNT(*) FROM llm_usage_hourly_dirty").fetchone()[0]
        focus = db.execute("SELECT definition,built_through FROM presence_focus_rollup_state WHERE id=1").fetchone()
        opened = presence_rollup.oldest_open_visit(db)
        covered = str(focus[1]) if focus is not None and focus[0] == presence_rollup.DEFINITION else ""
        return rollup_through(db), int(dirty), covered, str(opened or "")
    finally:
        db.close()


def _hours_behind(through: str, now: datetime) -> float | None:
    moment = presence_rollup.canonical(through) if through else None
    return None if moment is None else (now - moment).total_seconds() / 3600


def _shown(hours: float | None) -> str:
    return "all" if hours is None else f"{hours:.1f}"


async def report_lag(read, ended: str, steps: dict[str, int]) -> None:
    """Read coverage on ``read`` and warn when either rollup is more than ``LAG_WARNING`` behind."""
    clock = asyncio.get_running_loop().time()
    if clock - _last_warning[0] < _EVERY_SECONDS:
        return
    try:
        usage, dirty, focus, opened = await read(coverage_job)
    except Exception:
        logger.warning("rollup coverage read failed", exc_info=True)
        return
    now, limit = utc_now(), LAG_WARNING.total_seconds() / 3600
    behind = {"usage": _hours_behind(usage, now), "presence": _hours_behind(focus, now)}
    if all(hours is not None and hours <= limit for hours in behind.values()):
        return
    _last_warning[0] = clock
    logger.warning(
        "measurement rollup behind after refresh (%s; steps usage=%d presence=%d): usage built_through=%s "
        "hours_behind=%s dirty_hours=%d; presence built_through=%s hours_behind=%s oldest_open_visit=%s",
        ended, steps["usage"], steps["presence"], usage or "none", _shown(behind["usage"]), dirty,
        focus or "none", _shown(behind["presence"]), opened or "none",
    )
