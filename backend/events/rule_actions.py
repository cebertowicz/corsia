"""What each rule action takes, checked when a rule is written.

``RuleRefused`` is the one refusal a rule write raises; its text says why.
``check_script`` is the ``run_script`` action's check and ``check_worker`` the
``start_worker`` action's, over the workflows :func:`reachable_workflows` reads.
Evaluation and the run queue (``events.rules``, ``events.rule_runs``) never import
this module, so the writer's checks and the evaluator stay apart. Names no connector.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.skill_registry_types import SkillRow
    from events.connectors import EventTypeDecl


class RuleRefused(ValueError):
    """A request the writer limits or the rule shape refuse; the text says why."""


def check_script(db: sqlite3.Connection, writer: str, script_id: Any, extra: Mapping[str, Any]) -> str:
    """The script a ``run_script`` rule may run: one of its writer's own active script
    schedules. ``extra`` are the fields that belong to other actions, all refused."""
    given = sorted(name for name, value in extra.items() if value is not None)
    if given:
        raise RuleRefused(f"A run_script rule takes no {' or '.join(given)}: it runs your script, "
                          "and the event still reaches whoever it reaches today.")
    if not isinstance(script_id, str) or not script_id:
        raise RuleRefused("run_script needs script_id, the id automation_list shows for one of your "
                          "script schedules.")
    row = db.execute("SELECT thread_id, state FROM automation_schedules WHERE id=? AND kind='script'",
                     (script_id,)).fetchone()
    if row is None or row["thread_id"] != writer:
        raise RuleRefused(f"{script_id} isn't one of your script schedules; automation_list shows "
                          "yours, and automation_schedule registers one.")
    if row["state"] != "active":
        raise RuleRefused(f"Your script schedule {script_id} is {row['state']}; register the script "
                          "again with automation_schedule and use the new id.")
    return script_id


def reachable_workflows(db: sqlite3.Connection, thread_id: str, project_id: str | None,
                        workspace: Path) -> dict[str, SkillRow]:
    """The skills ``thread_id`` can read that carry a ``corsia.workflow``, by name.

    Reads skill folders, so call it outside a write transaction. A worker's workflow
    may declare a result and no steps, so this reads skills directly rather than the
    plan's step workflows.
    """
    from core.skill_resolution import resolve_effective_skills

    resolution = resolve_effective_skills(
        thread_id=thread_id, project_id=project_id, workspace=workspace, db=db)
    return {skill.name: skill for skill in resolution.included
            if isinstance(skill.corsia, dict) and isinstance(skill.corsia.get("workflow"), dict)}


def check_worker(
    workflows: Mapping[str, SkillRow], workflow_id: Any, identity_fact: Any,
    decl: EventTypeDecl, extra: Mapping[str, Any],
) -> tuple[str, str]:
    """The workflow a ``start_worker`` rule runs and the fact naming one request: a
    workflow its writer can read that declares a result, and a fact the event declares.
    ``extra`` are the fields that belong to other actions, all refused."""
    given = sorted(name for name, value in extra.items() if value is not None)
    if given:
        raise RuleRefused(f"A start_worker rule takes no {' or '.join(given)}: the worker is yours, "
                          "and its result comes back as a worker.result event on the item.")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise RuleRefused("start_worker needs workflow_id, the name of a skill you can read whose "
                          "corsia.yaml has a workflow with a result.")
    skill = workflows.get(workflow_id)
    if skill is None:
        known = ", ".join(sorted(workflows)) or "none"
        raise RuleRefused(f"{workflow_id} isn't a workflow you can read (yours: {known}); add the "
                          "skill with a corsia.workflow, or name one of these.")
    if "result" not in (skill.corsia or {}).get("workflow", {}):
        raise RuleRefused(f"Workflow {workflow_id} declares no result; add corsia.workflow.result, "
                          "the schema of the worker's final answer, to its corsia.yaml.")
    facts = (*decl.identity_facts, *decl.filterable_fields)
    if identity_fact not in facts:
        raise RuleRefused(f"request_identity_fact names the {decl.event_type} fact that tells one "
                          f"request from another: one of {sorted(set(facts))}.")
    return workflow_id, identity_fact
