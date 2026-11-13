"""Trusted plan writer for a person's document-comment task.

The task exists only while the person has comments waiting on a document and no open review
of that document carries them. ``reconcile`` is the one owner of that rule: it never creates
the task while an ask covers it, closes it when one opens, and creates it again when the last
one ends with comments still waiting. It sits in the plan of the document's current owner, and
moves when that owner changes. It runs where an input changes (``user_ask.comment_tasks_changed``),
when the owner's plan resumes, and closes as deleted when the document's file is gone
(``registry.models.retire``).
"""

import json
from pathlib import Path

from doceditor import person_comment_attention
from infra import write_effects
from infra.config import WORKSPACE
from plans import artifact_links, node_states, notifications, review_evidence, review_requests, service, user_ask
from plans.feedback_items import artifact_name
from registry import models as registry_models
from registry.identity_bridge import OWNER_REGISTRATION_STATES
from registry.shared_principals import document_scope

PROVENANCE = 'document.person_comment_task'
# Close reasons, one per cause; each stays true on the closed node.
COVERED = "Covered by this document’s review."
DELETED = 'This document is deleted.'
NO_OWNER = 'This document has no owner.'
PROMOTED = "This document is in another place, and its comments can’t be shown there. CORSIA keeps them."
# The retirement causes that end a document's task, each with its reason.
_RETIRED = {'file_gone': DELETED, 'promoted': PROMOTED}


def moved(owner_title: str) -> str:
    return f'This document belongs to {owner_title}.'


def _supervisor(db, artifact: dict) -> str | None:
    """Use the document's scope conductor until it has a per-document supervisor."""
    path = artifact.get('path') or artifact.get('external_id')
    if not path:
        return None
    return document_scope(path, workspace=WORKSPACE, db=db)[1]


def _owner(db, artifact: dict | None):
    """The current owner's thread row (the supervisor's for a person-owned document); none once released."""
    if not artifact or artifact.get('relationship_state') not in OWNER_REGISTRATION_STATES:
        return None
    owner = artifact.get('parent_thread_id')
    if owner == 'ui':
        owner = _supervisor(db, artifact)
    return db.execute('SELECT id, user_id, title FROM threads WHERE id=?', (owner,)).fetchone() if owner else None


def covering_asks(db, artifact_ids, user_id: str) -> dict[str, list[str]]:
    """The open asks raised to this person on each document, earliest raised first, by task id.

    Any agent's ask counts, whether its plan is paused or the person moved it to Later: it
    still carries the document's comments. Only an attention ask counts, so neither the
    comment task itself nor an owner's feedback task ever covers.
    """
    ids = list(dict.fromkeys(str(artifact_id) for artifact_id in artifact_ids))
    covering: dict[str, list[str]] = {artifact_id: [] for artifact_id in ids}
    if not ids or not user_id:
        return covering
    rows = db.execute(
        user_ask.open_asks_sql(
            'l.artifact_id, n.id AS node_id',
            joins="JOIN threads asker ON asker.id=p.owner_id AND p.owner_kind='thread'",
            by='all',
        ) + ' AND l.artifact_id IN (SELECT value FROM json_each(?)) AND asker.user_id=?'
        ' ORDER BY h.created_at, h.id',
        (json.dumps(ids), user_id),
    ).fetchall()
    for row in rows:
        asks = covering[str(row['artifact_id'])]
        if str(row['node_id']) not in asks:
            asks.append(str(row['node_id']))
    return covering


def is_covered(db, artifact_id: str, user_id: str) -> bool:
    return bool(covering_asks(db, [artifact_id], user_id)[artifact_id])


def comment_tasks(db, node_ids) -> dict[str, str]:
    """Each comment task among ``node_ids``, with the document it belongs to."""
    ids = [str(node_id) for node_id in node_ids]
    if not ids:
        return {}
    rows = db.execute(
        'SELECT node_id, artifact_id FROM plan_node_artifact_links'
        ' WHERE provenance=? AND node_id IN (SELECT value FROM json_each(?))',
        (PROVENANCE, json.dumps(ids)),
    ).fetchall()
    return {str(row['node_id']): str(row['artifact_id']) for row in rows}


def _open_task(db, artifact_id: str) -> tuple[str | None, str | None]:
    """The document's open task, in any plan, with the thread whose plan holds it."""
    row = db.execute(
        f"""SELECT n.id, p.owner_id FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id=l.node_id JOIN plans p ON p.id=n.plan_id
            WHERE l.artifact_id=? AND l.provenance=? AND {node_states.open_sql('n')}
            ORDER BY l.created_at DESC LIMIT 1""",
        (artifact_id, PROVENANCE),
    ).fetchone()
    return (str(row['id']), str(row['owner_id'])) if row else (None, None)


def _close(db, node_id: str, reason: str) -> bool:
    """Close the task without deciding or reading anything; a paused plan or held task stays."""
    if review_evidence.stop_reason(db, service._node_row(db, node_id)) is not None:
        return False
    review_requests.close_review_node(db, node_id, reason=reason, provenance=PROVENANCE)
    return True


def document_retired(db, artifact_id: str, cause: str | None) -> None:
    """Close the open task when the retirement ends its comments' place: the file is gone, or a promote
    tombstoned it without carrying the comments over (``reconcile`` reads the cause ``retire`` kept)."""
    if cause in _RETIRED:
        reconcile(db, artifact_id)


def _create(db, artifact_id: str, artifact: dict, owner: str) -> str:
    plan = service.ensure_plan('thread', owner, db=db)
    path = artifact.get('path') or artifact.get('external_id')
    title = Path(path).name if path else artifact_name(db, artifact_id)
    node = service.add_node(
        plan.id, kind='task', title=title, acceptance_criteria='Read the comments and decide the suggestions.',
        description='Open the document to read its comments and decide its suggestions.',
        assignee='user', actor_id=None, provenance=PROVENANCE, db=db, announce=False,
    )
    artifact_links.link(node.id, artifact_id, 'input', actor_id=None, provenance=PROVENANCE, db=db)
    write_effects.after_commit(db, lambda: notifications.notify_after_commit((plan.id,), db=db))
    return node.id


def reconcile(db, artifact_id: str) -> str | None:
    """Create, keep, move or end the one system-owned task against the live document and its asks."""
    node_id, holder = _open_task(db, artifact_id)
    state = registry_models.identity_state(artifact_id, db=db)
    if state is None:
        if node_id:
            _close(db, node_id, DELETED)
        return None
    ended = _RETIRED.get(registry_models.retired_cause(artifact_id, db=db) or '')
    if ended:
        # The comments' place is gone. A close a paused plan or held task deferred finishes on its resume.
        return node_id if node_id and not _close(db, node_id, ended) else None
    artifact = registry_models.get_artifact(artifact_id, db=db)
    owner = _owner(db, artifact)
    if not owner or not owner['user_id']:
        if node_id:
            _close(db, node_id, NO_OWNER)
        return None
    owner_id, user_id = str(owner['id']), str(owner['user_id'])
    count = sum(person_comment_attention.counts(db, artifact_id, user_id))
    if not count:
        if node_id:
            review_requests.settle_node(
                db, node_id, evidence='The document has no remaining comment attention.', provenance=PROVENANCE,
                verified_by=f'system:{PROVENANCE}',
                stopped_note='{evidence}', stopped_event='person_comment_attention_cleared_while_stopped',
            )
        return None
    if state != 'active':
        # Retired for a cause that leaves the document readable (the others close above).
        return node_id
    if node_id and holder != owner_id and _close(db, node_id, moved(owner['title'] or 'another agent')):
        node_id = None
    if is_covered(db, artifact_id, user_id):
        if node_id:
            _close(db, node_id, COVERED)
        return None
    return node_id or _create(db, artifact_id, artifact, owner_id)


def plan_resumed(db, plan_id: str) -> None:
    """Reconcile the documents of a resumed plan's open comment tasks, which a pause left as they were."""
    rows = db.execute(
        f"""SELECT DISTINCT l.artifact_id FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id=l.node_id
            WHERE n.plan_id=? AND l.provenance=? AND {node_states.open_sql('n')}""",
        (plan_id, PROVENANCE),
    ).fetchall()
    user_ask.comment_tasks_changed(db, [str(row['artifact_id']) for row in rows])
