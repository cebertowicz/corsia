"""System inputs about rule-worker waits."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from infra.database import write_transaction


def notice_wait(row: Mapping[str, Any]) -> None:
    """Tell the owner why its start waits, without settling it."""
    from events.rule_actions import reachable_workflows
    from infra.config import WORKSPACE
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.local_only import _is_cloud
    from thread.model_row import _display_model

    db = get_db()
    try:
        owner = db.execute("SELECT project_id FROM threads WHERE id=?", (row["thread_id"],)).fetchone()
        workflows = reachable_workflows(db, row["thread_id"], owner["project_id"] if owner else None, WORKSPACE)
        skill = workflows.get(row["workflow_id"])
        workflow = (skill.corsia or {}).get("workflow", {}) if skill else {}
        title = workflow.get("title") or row["workflow_id"]
        model = row["model"]
        name = _display_model(db, model) if model else "this chat's model"
        cloud = not model or _is_cloud(model, db=db)
    finally:
        db.close()
    policy = ("this project uses only local models" if row["cause"] == "project_cloud_off"
              else "this chat runs only on local models")
    if not model:
        subject = "this chat" if row["cause"] == "project_cloud_off" else "it"
        content = (f"{title} is waiting to start: {policy}, and {subject} has no model chosen. "
                   "It starts once this chat uses a local model that's ready.")
    elif cloud:
        content = (f"{title} is waiting to start: {policy}, and {name} is a cloud model. "
                   "It starts once this chat uses a local model that's ready.")
    else:
        content = (f"{title} is waiting to start: {name} isn't ready to run, and {policy}. "
                   f"It starts once {name} is ready.")
    with write_transaction(None, scope="worker_start_wait_notice") as conn:
        accept_thread_input(row["thread_id"], role="system", intent="apply_now",
                            message_id=str(uuid.uuid4()), content=content, db=conn)
