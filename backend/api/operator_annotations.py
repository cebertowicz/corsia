"""What the person's plan read carries beyond the plan's own state: comment attention and browser steps."""

from doceditor import person_comment_projection
from host_access.browser_steps import browser_threads
from plans import public


def annotate(db, envelope: dict, user_id: str) -> dict:
    """Mark comment attention, then each open browser step with the thread whose browser it is in."""
    envelope = person_comment_projection.annotate(db, envelope, user_id)
    mark_browser_steps(db, envelope)
    return envelope


def mark_browser_steps(db, envelope: dict) -> None:
    """Set ``browser_thread_id`` on each open person step a shared browser opened.

    The step's kind is the provenance its ``node_created`` event was stamped with, which never
    changes, so the read's revision token needs nothing added.
    """
    steps: dict[str, tuple[dict, str]] = {}
    for entry in envelope.get('plans', []):
        plan = entry.get('plan') or {}
        if plan.get('owner_kind') != 'thread':
            continue
        for node in entry.get('nodes', []):
            if node.get('assignee') == 'user' and node.get('state') not in public.TERMINAL_STATES:
                steps[str(node['id'])] = (node, str(plan['owner_id']))
    for node_id, thread_id in browser_threads(db, {node_id: owner for node_id, (_, owner) in steps.items()}).items():
        steps[node_id][0]['browser_thread_id'] = thread_id


def mark_activity_browser_steps(db, rows: list[dict]) -> list[dict]:
    """Set ``browser_thread_id`` on each Activity task row whose step a shared browser opened; return ``rows``.

    A task row's source is the thread whose plan holds the step, the same owner the plan read uses.
    """
    tasks = {row['detail']['node_id']: row for row in rows
             if row['kind'] == 'task' and row['source'].get('thread_id')}
    owners = {node_id: row['source']['thread_id'] for node_id, row in tasks.items()}
    for node_id, thread_id in browser_threads(db, owners).items():
        tasks[node_id]['detail']['browser_thread_id'] = thread_id
    return rows
