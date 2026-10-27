"""System inputs about rule-worker starts."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from infra.database import write_transaction


def notice_model(row: Mapping[str, Any], model: str) -> None:
    from infra.input_admission import accept_thread_input
    from thread.model_row import _display_model

    with write_transaction(None, scope="worker_start_model_notice") as conn:
        name = _display_model(conn, model)
        accept_thread_input(row["thread_id"], role="system", intent="apply_now",
                            message_id=str(uuid.uuid4()),
                            content=f"The workflow worker started on {name}.", db=conn)


def notice_hold(row: Mapping[str, Any]) -> None:
    """Tell the owner that this start is waiting, without settling it."""
    from events.rule_actions import reachable_workflows
    from infra.config import WORKSPACE
    from infra.database import get_db
    from infra.input_admission import accept_thread_input

    db = get_db()
    try:
        owner = db.execute("SELECT project_id FROM threads WHERE id=?", (row["thread_id"],)).fetchone()
        workflows = reachable_workflows(db, row["thread_id"], owner["project_id"] if owner else None, WORKSPACE)
        skill = workflows.get(row["workflow_id"])
        workflow = (skill.corsia or {}).get("workflow", {}) if skill else {}
        title = workflow.get("title") or row["workflow_id"]
    finally:
        db.close()
    with write_transaction(None, scope="worker_start_hold_notice") as conn:
        accept_thread_input(
            row["thread_id"], role="system", intent="apply_now", message_id=str(uuid.uuid4()),
            content=f"Held the start of {title}: this chat runs only on local models, and none is ready. It starts as soon as one is.",
            db=conn,
        )
