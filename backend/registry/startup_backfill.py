"""The run-once protocol the one-time pane-row passes share.

A pass retires rows through the shared retirement writer and reports how many
it retired per thread. This runner decides whether the pass still has to run
and records that it did: one marker setting per pass, written only after the
pass returns, so a pass that raises runs again on the next start.

A task settlement that cannot classify its rows forgets the markers of the two
passes that apply the settlement rules to tasks already settled, so the next
start retires what the settlement could not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from infra.database import get_db
from infra.settings import get_setting, set_setting
from registry.retirement_predicate import SharedRootsUnreadable

logger = logging.getLogger(__name__)

#: The passes that redo what task settlement does, for tasks already settled.
FINISHED_TASK_MARKER = "artifact_pane:finished_task_rows_retired"
TASKLESS_MARKER = "artifact_pane:taskless_rows_retired"


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


@contextmanager
def settlement_retirement(db) -> Iterator[None]:
    """Let a task settle when its rows cannot be classified, without losing them.

    A task settles once and a registry read must not fail it. When the shared
    roots cannot be read, the retirement inside this block writes nothing, and
    this forgets the two settlement passes in the same write: the next start
    whose roots read retires the rows this settlement left, and a start whose
    roots still do not read fails the pass and tries again.
    """
    try:
        yield
    except SharedRootsUnreadable:
        logger.warning(
            "task rows could not be classified at settlement; the next start retires them",
            exc_info=True,
        )
        db.executemany(
            "DELETE FROM settings WHERE key=?",
            [(FINISHED_TASK_MARKER,), (TASKLESS_MARKER,)],
        )
