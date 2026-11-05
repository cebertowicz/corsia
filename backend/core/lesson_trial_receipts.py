"""Read task-start trial delivery receipts separately from indexed offers."""
from __future__ import annotations

import json
from typing import Any

from core.lesson_report_sql import json_row_chunks

FIELDS = ("receipt_id", "thread_id", "turn_id", "task_id", "checkpoint_generation_id",
          "created_at", "lesson_id", "behavioral_sha256", "path", "raw_sha256")
PERIOD_SQL = (
    "SELECT g.id AS receipt_id,g.thread_id,g.turn_id,g.node_id AS task_id,"
    "g.checkpoint_generation_id,g.created_at,g.lesson_id,"
    "g.lesson_behavioral_sha256 AS behavioral_sha256,g.path,g.raw_sha256 "
    "FROM guidance_loads g JOIN threads t ON t.id=g.thread_id "
    "JOIN (SELECT value AS tid FROM json_each(?) LIMIT -1) o ON o.tid=t.id "
    "WHERE t.user_id=? AND g.variant='with' AND g.lesson_id IS NOT NULL "
    "AND g.lesson_behavioral_sha256 IS NOT NULL AND g.created_at>=? AND g.created_at<?"
)


def read_trial_deliveries(db, *, user_id: str, thread_ids: tuple[str, ...],
                          from_ts: str, to_ts: str) -> list[dict[str, Any]]:
    """Read same-user deliveries; indexed offers remain a separate report signal."""
    owners = list(dict.fromkeys(thread_ids))
    return list(json_row_chunks(db, PERIOD_SQL, (json.dumps(owners), user_id, from_ts, to_ts),
                                FIELDS, "created_at,receipt_id"))
