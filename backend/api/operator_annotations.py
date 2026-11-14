"""What the person's plan read carries beyond the plan's own state: comment attention and browser steps."""

import json

from doceditor import person_comment_projection
from host_access.browser_steps import PasswordStep, PersonStep
from plans import public

BROWSER_STEP_PROVENANCES = (PasswordStep.provenance, PersonStep.provenance)


def annotate(db, envelope: dict, user_id: str) -> dict:
    """Mark comment attention, then each open browser step with the thread whose browser it is in."""
    envelope = person_comment_projection.annotate(db, envelope, user_id)
    mark_browser_steps(db, envelope)
    return envelope


def mark_browser_steps(db, envelope: dict) -> None:
    """Set ``browser_thread_id`` on each open person step a shared browser opened.

    The step's kind is the provenance its ``node_created`` event was stamped with, which never
    changes, so the read's revision token needs nothing added. The thread is the plan's owner:
    a step lives in the plan of the thread whose browser opened it.
    """
    steps: dict[str, tuple[dict, str]] = {}
    for entry in envelope.get('plans', []):
        plan = entry.get('plan') or {}
        if plan.get('owner_kind') != 'thread':
            continue
        for node in entry.get('nodes', []):
            if node.get('assignee') == 'user' and node.get('state') not in public.TERMINAL_STATES:
                steps[str(node['id'])] = (node, str(plan['owner_id']))
    if not steps:
        return
    rows = db.execute(
        "SELECT node_id FROM plan_node_events WHERE event='node_created' AND provenance IN (?, ?)"
        " AND node_id IN (SELECT value FROM json_each(?))",
        (*BROWSER_STEP_PROVENANCES, json.dumps(list(steps))),
    ).fetchall()
    for row in rows:
        node, thread_id = steps[str(row['node_id'])]
        node['browser_thread_id'] = thread_id
