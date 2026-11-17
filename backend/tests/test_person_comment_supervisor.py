"""Person-owned comment tasks follow the document's visible supervisor."""

import pytest

from doc_feedback_helpers import add_comment
from infra.database import get_db, write_transaction
from plans import person_comment_task
from registry import models
from registry.document_ownership_change import OwnershipBody, apply_change
from tests.test_document_owner_supervisor import owned_document as owned_document, _add_agent


@pytest.mark.parametrize('person_owner', ['ui', 'project:project-alpha'])
def test_task_moves_when_person_changes_supervisor(owned_document, person_owner):
    _, artifact_id = owned_document
    _add_agent('agent-2', 'Second agent')
    db = get_db()
    try:
        artifact = models.get_artifact(artifact_id, db=db)
        with write_transaction(db, scope='test_person_owner') as conn:
            models.rehome_artifact(integration=artifact['integration'], type=artifact['type'],
                external_id=artifact['external_id'], new_owner_thread_id=person_owner,
                actor_thread_id=None, authority='system', reason='test', source_path='test', db=conn)
            artifact = models.get_artifact(artifact_id, db=conn)
            apply_change(conn, artifact, OwnershipBody(supervisor_thread_id='agent-1'), person_owner)
        root, _ = add_comment(db, artifact_id, author_kind='user', author_id=None)
        add_comment(db, artifact_id, author_id='agent-1', parent_id=root['id'])
        with write_transaction(db, scope='test_task_first') as conn:
            first = person_comment_task.reconcile(conn, artifact_id)
        assert person_comment_task._open_task(db, artifact_id) == (first, 'agent-1')
        with write_transaction(db, scope='test_task_move') as conn:
            artifact = models.get_artifact(artifact_id, db=conn)
            apply_change(conn, artifact, OwnershipBody(supervisor_thread_id='agent-2'), person_owner)
        second, holder = person_comment_task._open_task(db, artifact_id)
        assert second and second != first
        assert holder == 'agent-2'
        assert db.execute('SELECT state FROM plan_nodes WHERE id=?', (first,)).fetchone()[0] == 'closed'
    finally:
        db.close()
