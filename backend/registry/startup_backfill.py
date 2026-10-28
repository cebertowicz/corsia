"""The run-once protocol the one-time pane-row passes share.

A pass retires rows through the shared retirement writer and reports how many
it retired per thread. This runner decides whether the pass still has to run
and records that it did: one marker setting per pass, written only after the
pass returns, so a pass that raises runs again on the next start.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from infra.database import get_db
from infra.settings import get_setting, set_setting

logger = logging.getLogger(__name__)

#: The passes that redo what task settlement does, for tasks already settled.
#: Renamed when shared documents stopped outliving their task, so each pass
#: runs once more for the shared rows earlier settlements left behind.
FINISHED_TASK_MARKER = "artifact_pane:finished_task_rows_retired:shared_task_scoped"
TASKLESS_MARKER = "artifact_pane:taskless_rows_retired:shared_task_scoped"


def run_once(marker: str, label: str, retire: Callable[[object], dict[str, int]]) -> None:
    """Run ``retire`` once per instance on one explicitly closed connection."""
    db = get_db()
    try:
        if get_setting(marker, db=db):
            return
        retired = retire(db)
        for thread_id, count in sorted(retired.items()):
            logger.info("%s retired: thread %s, %d rows", label, thread_id, count)
        set_setting(
            marker,
            {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "retired_rows": sum(retired.values()),
                "retired_by_thread": retired,
            },
            db=db,
        )
    finally:
        db.close()
