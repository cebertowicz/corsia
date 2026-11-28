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
        shown_reply = {'kind': 'document', 'detail': {'artifact_id': doc, 'threads': [{'root_id': root['id'], 'reply_id': reply['id']}]}}
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


def test_person_task_descriptions_are_in_activity_rows(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        with write_transaction(db, scope='task_descriptions') as conn:
            plan = service.ensure_plan('thread', 'owner', db=conn)
            browser = service.add_node(plan.id, kind='task', title='Browser step', acceptance_criteria='Done',
                                       description='Enter your password.', assignee='user', actor_id=None,
                                       provenance='browser.password_guard', db=conn)
            ordinary = service.add_node(plan.id, kind='task', title='Ordinary task', acceptance_criteria='Done',
                                        description='Follow these instructions.', assignee='user', actor_id=None,
                                        provenance='test', db=conn)
        details = {row['detail']['node_id']: row['detail'] for row in projection.rows(db, 'owner-token', [])
                   if row['kind'] == 'task'}
        assert details[browser.id]['description'] == 'Enter your password.'
        assert details[ordinary.id]['description'] == 'Follow these instructions.'
    finally:
        db.close()


def test_activity_rows_name_the_browser_of_a_shared_browser_step(isolated_db):
    """The Activity queue read marks the step a shared browser opened, as the plan read does; nothing else."""
    from api.attention import activity_lists
    db = get_db()
    try:
        seed_thread(db, 'owner')
        with write_transaction(db, scope='browser_rows') as conn:
            plan = service.ensure_plan('thread', 'owner', db=conn)
            browser = service.add_node(plan.id, kind='task', title='Browser step', acceptance_criteria='Done',
                                       assignee='user', actor_id=None, provenance='browser.person_step', db=conn)
            ordinary = service.add_node(plan.id, kind='task', title='Ordinary task', acceptance_criteria='Done',
                                        assignee='user', actor_id=None, provenance='test', db=conn)
        tasks, _, _, _ = activity_lists(db, 'owner-token', view='activity')
        details = {row['detail']['node_id']: row['detail'] for row in tasks if row['kind'] == 'task'}
        assert details[browser.id]['browser_thread_id'] == 'owner'
        assert 'browser_thread_id' not in details[ordinary.id]
    finally:
        db.close()


def _task_keys(db):
    return [r['detail']['node_id'] for r in projection.rows(db, 'owner-token', []) if r['kind'] == 'task']


def test_later_person_task_has_no_row(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        with write_transaction(db, scope='later_task') as conn:
            plan = service.ensure_plan('thread', 'owner', db=conn)
            node = service.add_node(plan.id, kind='task', title='Person task', acceptance_criteria='Done',
                                    assignee='user', actor_id=None, provenance='test', db=conn)
        assert _task_keys(db) == [node.id]
        with write_transaction(db, scope='later_task_set') as conn:
            conn.execute("UPDATE plan_nodes SET later=1 WHERE id=?", (node.id,))
        assert _task_keys(db) == []
    finally:
        db.close()


def test_ask_from_later_node_has_no_review_row(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        doc = seed_document(db, 'owner')
        with write_transaction(db, scope='later_ask') as conn:
            node = _ask_node(conn, 'owner')
            _raise(conn, node.id, doc, 'owner')

        def reviews():
            annotated = public.annotate_attention(_authorized_raised(None, 'owner-token', db=db), db=db)
            return [r for r in projection.rows(db, 'owner-token', annotated) if r['kind'] == 'review']
        assert len(reviews()) == 1
        with write_transaction(db, scope='later_ask_set') as conn:
            conn.execute("UPDATE plan_nodes SET later=1 WHERE id=?", (node.id,))
        assert reviews() == []
    finally:
        db.close()


def test_person_step_row_opens_its_instructions_document(isolated_db):
    """A person step given an instructions document opens it from Activity, as its plan row does."""
    from plans import artifact_links, user_step_instructions
    db = get_db()
    try:
        seed_thread(db, 'owner')
        doc = seed_document(db, 'owner', name='runner-steps.md')
        with write_transaction(db, scope='step_instructions') as conn:
            plan = service.ensure_plan('thread', 'owner', db=conn)
            step = service.add_node(plan.id, kind='task', title='Start the runner', acceptance_criteria='Done',
                                    description='Steps in the linked doc.', assignee='user', actor_id=None,
                                    provenance='test', db=conn)
            bare = service.add_node(plan.id, kind='task', title='No document', acceptance_criteria='Done',
                                    assignee='user', actor_id=None, provenance='test', db=conn)
            artifact_links.link(step.id, doc, 'input', actor_id='owner',
                                provenance=user_step_instructions.PROVENANCE, db=conn)
        rows = {r['detail']['node_id']: r for r in projection.rows(db, 'owner-token', []) if r['kind'] == 'task'}
        assert rows[step.id]['open'] == {'artifact_id': doc}
        assert 'comment_artifact_id' not in rows[step.id]['detail']
        assert rows[step.id]['detail']['description'] == 'Steps in the linked doc.'
        assert rows[bare.id]['open'] is None
        assert len(projection.dedupe_comment_tasks(db, 'owner-token', list(rows.values()), [])) == 2
    finally:
        db.close()


def _step_with_doc(conn, plan, title, doc):
    from plans import artifact_links, user_step_instructions
    step = service.add_node(plan.id, kind='task', title=title, acceptance_criteria='Done',
                            assignee='user', actor_id=None, provenance='test', db=conn)
    artifact_links.link(step.id, doc, 'input', actor_id='owner',
                        provenance=user_step_instructions.PROVENANCE, db=conn)
    return step


def test_person_step_row_keeps_its_card_when_the_document_is_refused(isolated_db, monkeypatch):
    """A document the access rule refuses is never opened; the row keeps its own card and Done."""
    db = get_db()
    try:
        seed_thread(db, 'owner')
        doc = seed_document(db, 'owner', name='runner-steps.md')
        with write_transaction(db, scope='step_instructions_refused') as conn:
            step = _step_with_doc(conn, service.ensure_plan('thread', 'owner', db=conn), 'Start the runner', doc)
        monkeypatch.setattr(projection, 'may_access_artifact', lambda *a, **k: False)
        rows = {r['detail']['node_id']: r for r in projection.rows(db, 'owner-token', []) if r['kind'] == 'task'}
        assert rows[step.id]['open'] is None
    finally:
        db.close()


def test_shared_instructions_document_opens_only_from_the_step_its_bar_settles(isolated_db):
    """Two open steps given one document: only the step whose Done the bar shows opens it."""
    from plans import public
    db = get_db()
    try:
        seed_thread(db, 'owner')
        doc = seed_document(db, 'owner', name='shared-steps.md')
        with write_transaction(db, scope='step_instructions_shared') as conn:
            plan = service.ensure_plan('thread', 'owner', db=conn)
            older = _step_with_doc(conn, plan, 'Older step', doc)
        with write_transaction(db, scope='step_instructions_shared_2') as conn:
            newer = _step_with_doc(conn, service.ensure_plan('thread', 'owner', db=conn), 'Newer step', doc)
        bar_node = public.person_step_instructions(doc, db=db)['node_id']
        rows = {r['detail']['node_id']: r for r in projection.rows(db, 'owner-token', []) if r['kind'] == 'task'}
        assert {n: rows[n]['open'] for n in (older.id, newer.id)} == {
            n: ({'artifact_id': doc} if n == bar_node else None) for n in (older.id, newer.id)}
        assert sum(rows[n]['open'] is not None for n in (older.id, newer.id)) == 1
    finally:
        db.close()
