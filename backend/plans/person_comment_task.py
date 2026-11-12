"""Trusted plan writer for a person's document-comment task.

The task exists only while the person has comments waiting on a document and no open review
of that document carries them. ``reconcile`` is the one owner of that rule: it never creates
the task while an ask covers it, closes it when one opens, and creates it again when the last
one ends with comments still waiting. It runs after the asks on a document change
(``user_ask.asks_changed``) and when the document owner's plan resumes.
"""

import json
from pathlib import Path

from doceditor import person_comment_attention
from infra import write_effects
from infra.config import WORKSPACE
from plans import artifact_links, node_states, notifications, review_evidence, review_requests, service, user_ask
from plans.feedback_items import artifact_name
from registry import models as registry_models
from registry.shared_principals import document_scope

PROVENANCE = 'document.person_comment_task'
COVERED = 'An open review of this document now carries these comments.'


def _supervisor(db, artifact: dict) -> str | None:
    """Use the document's scope conductor until it has a per-document supervisor."""
    path = artifact.get('path') or artifact.get('external_id')
    if not path:
        return None
    return document_scope(path, workspace=WORKSPACE, db=db)[1]


def _owner_and_person(db, artifact: dict) -> tuple[str | None, str | None]:
    owner = artifact.get('parent_thread_id')
    if owner == 'ui':
        owner = _supervisor(db, artifact)
    row = db.execute('SELECT user_id FROM threads WHERE id=?', (owner,)).fetchone() if owner else None
    return (str(owner) if owner else None, str(row['user_id']) if row and row['user_id'] else None)


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


def _open_task(db, artifact_id: str) -> str | None:
    row = db.execute(
        f"""SELECT n.id FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id=l.node_id
            WHERE l.artifact_id=? AND l.provenance=? AND {node_states.open_sql('n')}
            ORDER BY l.created_at DESC LIMIT 1""",
        (artifact_id, PROVENANCE),
    ).fetchone()
    return str(row['id']) if row else None


def _close(db, node_id: str, reason: str) -> None:
    """Close the task without deciding or reading anything; a paused plan or held task stays."""
    if review_evidence.stop_reason(db, service._node_row(db, node_id)) is not None:
        return
    review_requests.close_review_node(db, node_id, reason=reason, provenance=PROVENANCE)


def _create(db, artifact_id: str, artifact: dict, owner: str) -> str:
    plan = service.ensure_plan('thread', owner, db=db)
    path = artifact.get('path') or artifact.get('external_id')
    title = Path(path).name if path else artifact_name(db, artifact_id)
    node = service.add_node(
        plan.id, kind='task', title=title, acceptance_criteria='Read the comments and decide the suggestions.',
        description='Open the document to read its comments and decide its suggestions.',
        assignee='user', actor_id=None, provenance=PROVENANCE, db=db,
    )
    artifact_links.link(node.id, artifact_id, 'input', actor_id=None, provenance=PROVENANCE, db=db)
    write_effects.after_commit(db, lambda: notifications.notify_after_commit((plan.id,), db=db))
    return node.id


def reconcile(db, artifact_id: str) -> str | None:
    """Create, keep or end the one system-owned task against the live document and its asks."""
    node_id = _open_task(db, artifact_id)
    artifact = registry_models.get_artifact(artifact_id, db=db)
    owner, user_id = _owner_and_person(db, artifact) if artifact else (None, None)
    if not owner or not user_id:
        if node_id:
            _close(db, node_id, 'The document is gone.' if not artifact
                   else 'The document has no owner or person to carry this task.')
        return None
    count = sum(person_comment_attention.counts(db, artifact_id, user_id))
    if not count:
        if node_id:
            review_requests.settle_node(
                db, node_id, evidence='The document has no remaining comment attention.', provenance=PROVENANCE,
                verified_by=f'system:{PROVENANCE}',
                stopped_note='{evidence}', stopped_event='person_comment_attention_cleared_while_stopped',
            )
        return None
    if is_covered(db, artifact_id, user_id):
        if node_id:
            _close(db, node_id, COVERED)
        return None
    return node_id or _create(db, artifact_id, artifact, owner)


def plan_resumed(db, plan_id: str) -> None:
    """Reconcile the documents of a resumed plan's open comment tasks, which a pause left as they were."""
    rows = db.execute(
        f"""SELECT DISTINCT l.artifact_id FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id=l.node_id
            WHERE n.plan_id=? AND l.provenance=? AND {node_states.open_sql('n')}""",
        (plan_id, PROVENANCE),
    ).fetchall()
    user_ask.asks_changed(db, [str(row['artifact_id']) for row in rows])
