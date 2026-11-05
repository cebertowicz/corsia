"""Read-only restart operation queries."""

from statistics import median

from automation.restart_rule import parse as _parse
from infra.database import get_db


def usual_update_seconds(*, db=None) -> int | None:
    """Return the median duration of the ten most recent completed updates."""
    connection = db or get_db()
    try:
        rows = connection.execute(
            "SELECT requested_at,settled_at FROM self_restart_requests "
            "WHERE kind='update' AND status='complete' "
            "ORDER BY requested_at DESC,id DESC LIMIT 10"
        ).fetchall()
        durations = [
            (settled - requested).total_seconds()
            for row in rows
            if (requested := _parse(row["requested_at"])) is not None
            and (settled := _parse(row["settled_at"])) is not None
            and settled >= requested
        ]
        return int(median(durations)) if durations else None
    finally:
        if db is None:
            connection.close()
