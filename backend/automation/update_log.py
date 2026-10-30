"""Attach lifecycle logs to their update operations."""

import json

from automation.self_restart import _merged_detail
from infra.database import write_transaction


def record_update_log(operation_id: str, log_path: str, *, db=None) -> bool:
    """Attach an already-created log to the exact in-flight update."""
    with write_transaction(db, scope="restart_operation") as connection:
        detail = _merged_detail(connection, operation_id, json.dumps({"log_path": log_path}))
        result = connection.execute(
            "UPDATE self_restart_requests SET failure_detail=? "
            "WHERE id=? AND kind='update' AND status='in_flight'",
            (detail, operation_id),
        )
        return result.rowcount == 1
