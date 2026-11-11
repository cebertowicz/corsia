"""Trusted plan writer for a person's document-comment task."""

from pathlib import Path

from doceditor import person_comment_attention
from infra.config import WORKSPACE
from infra.write_effects import after_commit
from plans import artifact_links, node_states, notifications, review_requests, service
from plans.feedback_items import artifact_name
from registry import models as registry_models
from registry.shared_principals import document_scope

PROVENANCE = 'document.person_comment_task'


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


def reconcile(db, artifact_id: str) -> str | None:
    """Create or settle the one system-owned task against the live document state."""
    artifact = registry_models.get_artifact(artifact_id, db=db)
    if not artifact:
        return None
    owner, user_id = _owner_and_person(db, artifact)
    if not owner or not user_id:
        return None
    row = db.execute(
        f"""SELECT n.id FROM plan_node_artifact_links l
             JOIN plan_nodes n ON n.id=l.node_id
            WHERE l.artifact_id=? AND l.provenance=? AND {node_states.open_sql('n')}
            ORDER BY l.created_at DESC LIMIT 1""",
        (artifact_id, PROVENANCE),
    ).fetchone()
    node_id = str(row['id']) if row else None
    count = sum(person_comment_attention.counts(db, artifact_id, user_id))
    if count and node_id is None:
        plan = service.ensure_plan('thread', owner, db=db)
        path = artifact.get('path') or artifact.get('external_id')
        title = Path(path).name if path else artifact_name(db, artifact_id)
        node = service.add_node(
            plan.id, kind='task', title=title, acceptance_criteria='Read the comments and decide the suggestions.',
            description='Open the document to read its comments and decide its suggestions.',
            assignee='user', actor_id=None, provenance=PROVENANCE, db=db,
        )
        artifact_links.link(node.id, artifact_id, 'input', actor_id=None, provenance=PROVENANCE, db=db)
        after_commit(db, lambda: notifications.notify_after_commit((plan.id,), db=db))
        return node.id
    if not count and node_id:
        review_requests.settle_node(
            db, node_id, evidence='The document has no remaining comment attention.', provenance=PROVENANCE,
            verified_by='system:document.person_comment_task',
            stopped_note='{evidence}', stopped_event='person_comment_attention_cleared_while_stopped',
        )
        return None
    return node_id
