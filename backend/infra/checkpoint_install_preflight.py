"""Read normal checkpoint-install snapshots before the writer starts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from infra.database import get_db


InstallPreflight = tuple[str | None, str]


def prepare_install_preflight(
    checkpoint_id: str,
    installed_at: str,
    db: sqlite3.Connection | None,
    required_record: Callable,
) -> tuple[InstallPreflight | None, Exception | None]:
    """Capture normal-install views, deferring errors until eligibility is known.

    An outer transaction remains authoritative and captures at commit. A
    preflight failure cannot select a stale checkpoint's terminal outcome, so
    the installer replays it only after eligibility is confirmed.
    """
    if db is not None and db.in_transaction:
        # An enclosing transaction remains the atomic snapshot boundary.
        return None, None
    try:
        connection = db if db is not None else get_db()
        record = required_record(connection, checkpoint_id)
        from infra.checkpoint_receipts import _presence_line_at
        from infra.checkpoint_snapshots import capture_artifact_snapshot

        return (
            _presence_line_at(connection, record.thread_id, record.id, installed_at),
            capture_artifact_snapshot(connection, record.thread_id),
        ), None
    except Exception as error:
        return None, error



def finish_preflight(
    preflight: InstallPreflight | None,
    record,
    thread_row: sqlite3.Row,
    installed_at: str,
    connection: sqlite3.Connection,
) -> None:
    """Publish the exact presence value frozen before the normal writer."""
    from infra.checkpoint_receipts import publish_install

    model = str(thread_row["model"] or "")
    if preflight is None:
        publish_install(record, model, installed_at, connection)
    else:
        publish_install(
            record, model, installed_at, connection, presence_line=preflight[0],
        )
