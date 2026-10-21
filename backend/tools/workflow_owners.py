"""Who owns each of an agent's effective workflow skills: the one rule measure_sync, script
schedules and the script runner share.

A workflow is usable only if it is among the agent's effective workflow skills. A thread-scope
skill (``.threads/<agent>/skills/...``) is owned by that thread, and an owner key naming another
thread is a conflict; any other scope's skill is owned by its ``workflow.owner`` key in
``corsia.yaml``, and with none it has no owner. Nothing is stored: the rule is read fresh each time.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from infra.measure_registry import WorkflowOwner, workflow_problem
from tools.workflow_task_fields import _workflow_version, effective_workflows


def workflow_owners(thread_id: str, project_id: str | None, db: Any) -> dict[str, WorkflowOwner]:
    """The agent's effective workflow skills, each with its owner under the rule above."""
    workflows = effective_workflows(SimpleNamespace(thread_id=thread_id, project_id=project_id), db)
    owners: dict[str, WorkflowOwner] = {}
    for name, workflow in workflows.items():
        skill = workflow.skill
        data = skill.corsia.get("workflow") if isinstance(skill.corsia, dict) else None
        key = data.get("owner") if isinstance(data, dict) else None
        key = str(key) if key not in (None, "") else None
        folder = Path(skill.path).parent
        if skill.scope == "thread":
            owners[name] = WorkflowOwner(folder, skill.scope_id, key if key not in (None, skill.scope_id) else None, skill)
        else:
            owners[name] = WorkflowOwner(folder, key, None, skill)
    return owners


def thread_workflow_owners(thread_id: str, db: Any) -> dict[str, WorkflowOwner]:
    """:func:`workflow_owners` for a thread, reading its project from its row."""
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    return workflow_owners(thread_id, row[0] if row else None, db)


def workflow_stamp(thread_id: str, workflow: str, db: Any) -> tuple[str, str | None] | None:
    """The (workflow_ref, workflow_version) a run of ``thread_id`` stamps for ``workflow``.

    None when the agent no longer owns it; the version is None when Git can't name one. Reads
    Git, so run it off the database pool.
    """
    owners = thread_workflow_owners(thread_id, db)
    if workflow_problem(owners, thread_id, workflow):
        return None
    return workflow, _workflow_version(owners[workflow].skill)
