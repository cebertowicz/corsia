from plans import activity_rows as projection

from infra.database import get_db, write_transaction
from doc_feedback_helpers import seed_thread, seed_document, add_comment
from plans import service, person_comment_task, public
from api.attention import _authorized_raised
from test_person_comment_attention import _ask_node, _raise


def test_rows_and_comment_overlap(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        doc = seed_document(db, 'owner')
        root, _ = add_comment(db, doc, author_kind='user', author_id=None)
        reply, _ = add_comment(db, doc, author_id='owner', parent_id=root['id'])
        with write_transaction(db, scope='test_rows') as conn:
            task = person_comment_task.reconcile(conn, doc)
        projected = projection.rows(db, 'owner-token', [])
        assert any(r['detail']['node_id'] == task for r in projected if r['kind'] == 'task')
        assert len(projection.dedupe_comment_tasks(db, 'owner-token', projected, [])) == 1
        shown_reply = {'kind': 'reply', 'detail': {'artifact_id': doc, 'root_id': root['id'], 'reply_id': reply['id']}}
        assert not projection.dedupe_comment_tasks(db, 'owner-token', projected, [shown_reply])
        with write_transaction(db, scope='test_ask') as conn:
            node = _ask_node(conn, 'owner')
            hold = _raise(conn, node.id, doc, 'owner')
        annotated = public.annotate_attention(_authorized_raised(None, 'owner-token', db=db), db=db)
        reviews = [r for r in projection.rows(db, 'owner-token', annotated) if r['kind'] == 'review']
        assert len(reviews) == 1
        assert reviews[0]['detail']['hold_ids'] == [hold]
        assert reviews[0]['open'] == {'artifact_id':doc}
    finally:
        db.close()


def test_scope_owned_tasks_have_no_user_settlement_route(isolated_db):
    db = get_db()
    try:
        with write_transaction(db, scope='test_scope_tasks') as conn:
            from infra.config import WORKSPACE_SELECTION
            conn.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('project-test','Project','t','t')")
            workspace = service.ensure_plan('workspace_scope', WORKSPACE_SELECTION.workspace_id, db=conn)
            project = service.ensure_plan('project_scope', 'project-test', db=conn)
            for plan in (workspace, project):
                service.add_node(plan.id, kind='task', title='Person task', acceptance_criteria='Done',
                                 assignee='user', actor_id=None, provenance='test', db=conn)
        assert projection.rows(db, 'owner-token', []) == []
    finally:
        db.close()
